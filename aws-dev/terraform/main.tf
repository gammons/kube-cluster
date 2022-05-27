terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 3.27"
    }
  }

  required_version = ">= 0.14.9"
}

provider "aws" {
  profile = "default"
  region  = "us-east-2"
}

resource "aws_instance" "dev_server" {
  ami           = "ami-09d58b77d2efa5a89"
  instance_type = "c6a.2xlarge"
  availability_zone = "us-east-2a"
  security_groups = ["default"]
  key_name = "id_rsa"

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
  }

  tags = {
    Name = "dev server"
  }
}

resource "aws_eip_association" "grant-dev" {
  instance_id = aws_instance.dev_server.id
  allocation_id = "eipalloc-0b00d5fd6795e85d7"
}

