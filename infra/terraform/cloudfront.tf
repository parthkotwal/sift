check "cloudfront_viewer_allowlist_uses_host_cidrs" {
  assert {
    condition     = alltrue([for cidr in var.alb_ingress_cidrs : endswith(cidr, "/32")])
    error_message = "CloudFront viewer enforcement requires every alb_ingress_cidrs entry to be an IPv4 /32."
  }
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "random_password" "cloudfront_origin_header" {
  length  = 32
  special = false
}

resource "aws_cloudfront_function" "viewer_allowlist" {
  name    = "${local.name_prefix}-viewer-allowlist"
  runtime = "cloudfront-js-2.0"
  comment = "Restrict the ephemeral Sift endpoint to approved viewer IPs"
  publish = true
  code    = <<-JAVASCRIPT
    function handler(event) {
      var allowed = ${jsonencode([for cidr in var.alb_ingress_cidrs : trimsuffix(cidr, "/32")])};
      if (allowed.indexOf(event.viewer.ip) === -1) {
        return {
          statusCode: 403,
          statusDescription: 'Forbidden',
          headers: {
            'content-type': { value: 'application/json' },
            'cache-control': { value: 'no-store' }
          },
          body: '{"detail":"Forbidden"}'
        };
      }
      return event.request;
    }
  JAVASCRIPT
}

resource "aws_cloudfront_distribution" "api" {
  enabled         = true
  is_ipv6_enabled = false
  comment         = "Restricted HTTPS front door for the ephemeral Sift showcase"
  price_class     = "PriceClass_100"
  http_version    = "http2and3"

  origin {
    domain_name = aws_lb.api.dns_name
    origin_id   = "sift-alb"

    custom_header {
      name  = "X-Sift-Origin-Verify"
      value = random_password.cloudfront_origin_header.result
    }

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_keepalive_timeout = 5
      origin_read_timeout      = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "sift-alb"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.viewer_allowlist.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  depends_on = [aws_lb_listener_rule.cloudfront_origin]

  tags = {
    Name = "${local.name_prefix}-https"
  }
}
