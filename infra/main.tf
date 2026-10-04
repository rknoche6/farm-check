# AWS deployment sketch: the API on ECS Fargate behind an ALB, PostGIS on RDS.
# Validated with `terraform validate`; it has NOT been applied to an AWS account.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" { region = var.region }

variable "region" { default = "eu-central-1" }
variable "image" { description = "API image, e.g. <account>.dkr.ecr.eu-central-1.amazonaws.com/farm-check:<sha>" }
variable "db_password" { sensitive = true }

data "aws_availability_zones" "az" { state = "available" }

module "vpc" {
  source             = "terraform-aws-modules/vpc/aws"
  version            = "~> 6.0"
  name               = "farm-check"
  cidr               = "10.40.0.0/16"
  azs                = slice(data.aws_availability_zones.az.names, 0, 2)
  public_subnets     = ["10.40.1.0/24", "10.40.2.0/24"]
  private_subnets    = ["10.40.11.0/24", "10.40.12.0/24"]
  enable_nat_gateway = true
  single_nat_gateway = true
}

# --- security groups: internet -> ALB -> tasks -> RDS -------------------------
resource "aws_security_group" "alb" {
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "task" {
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }
}

# --- PostGIS on RDS (postgis and postgis_raster are supported extensions) -----
resource "aws_db_subnet_group" "db" {
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "db" {
  identifier             = "farm-check"
  engine                 = "postgres"
  engine_version         = "17"
  instance_class         = "db.t4g.medium"
  allocated_storage      = 50
  db_name                = "farmcheck"
  username               = "farm"
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.db.name
  vpc_security_group_ids = [aws_security_group.db.id]
  storage_encrypted      = true
  skip_final_snapshot    = true
}

resource "aws_secretsmanager_secret" "db_url" { name = "farm-check/database-url" }

resource "aws_secretsmanager_secret_version" "db_url" {
  secret_id     = aws_secretsmanager_secret.db_url.id
  secret_string = "postgresql://farm:${var.db_password}@${aws_db_instance.db.address}:5432/farmcheck"
}

# --- ECS Fargate service -----------------------------------------------------
resource "aws_ecs_cluster" "main" { name = "farm-check" }

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/farm-check"
  retention_in_days = 14
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "exec" {
  name               = "farm-check-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "read_secret" {
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.db_url.arn }]
  })
}

resource "aws_ecs_task_definition" "api" {
  family                   = "farm-check-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.exec.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  container_definitions = jsonencode([{
    name         = "api"
    image        = var.image
    portMappings = [{ containerPort = 8000 }]
    secrets      = [{ name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.db_url.arn }]
    healthCheck  = { command = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')\""] }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_lb" "api" {
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "api" {
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = module.vpc.vpc_id
  health_check { path = "/healthz" }
}

resource "aws_ecs_service" "api" {
  name            = "farm-check-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 2
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = module.vpc.private_subnets
    security_groups = [aws_security_group.task.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
}

# The HTTPS listener needs an ACM certificate for your domain:
#   resource "aws_lb_listener" "https" { load_balancer_arn = aws_lb.api.arn, port = 443, protocol = "HTTPS",
#     certificate_arn = <acm cert>, default_action { type = "forward", target_group_arn = aws_lb_target_group.api.arn } }

output "alb_dns_name" { value = aws_lb.api.dns_name }
output "db_address" { value = aws_db_instance.db.address }
