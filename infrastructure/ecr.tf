resource "aws_ecr_repository" "gateway" {
  name                 = "${var.name}/gateway"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}
