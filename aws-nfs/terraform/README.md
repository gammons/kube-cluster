# Terraform NFS server setup

Use this to provision a barebones ubuntu server, ready to be provisioned as an NFS server.

It will create the EC2 instance and attach the 100gb block store in `/dev/sdvh`

### Prerequisites

1. Ensure you have the AWS CLI installed and it is configured.
1. Ensure you have `terraform` installed.
2. Run `terraform apply`.
