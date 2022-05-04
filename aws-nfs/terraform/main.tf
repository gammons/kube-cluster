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

resource "aws_volume_attachment" "nfs_att" {
  device_name = "/dev/sdh"
  volume_id   = "vol-002db845710507df8"
  instance_id = aws_instance.nfs_server.id
}

resource "aws_instance" "nfs_server" {
  ami           = "ami-0aeb7c931a5a61206"
  instance_type = "t2.nano"
  availability_zone = "us-east-2a"
  security_groups = ["default"]
  key_name = "id_rsa"

  tags = {
    Name = "nfs server"
  }
}
