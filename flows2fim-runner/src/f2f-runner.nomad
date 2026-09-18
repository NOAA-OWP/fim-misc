job "f2f-runner" {
  datacenters = ["dc1"]
  type = "batch"
  constraint {
    attribute = "${node.class}"
    value     = "linux"
  }  
  parameterized {
    meta_required = [
      "dir_name", 
      "flow_file", 
      "return_period", 
      "registry_token"
    ]
    meta_optional = [
      "aws_access_key", 
      "aws_secret_key",
      "aws_session_token"
    ]
  }
  
  group "f2f-processor" {
    # Don't attempt restart since don't want to retry on most errors
    restart {
      attempts = 0
      mode = "fail"
    }
    task "processor" {
      driver = "docker"
      
      config {
        image = "registry.sh.nextgenwaterprediction.com/ngwpc/fim-c/flows2fim_extents"
        force_pull = true
        auth {
          username = "ReadOnly_NGWPC_Group_Deploy_Token"
          password = "${NOMAD_META_registry_token}"
        }
        args = [
          "${NOMAD_META_dir_name}",
          "${NOMAD_META_flow_file}",
          "${NOMAD_META_return_period}"
        ]
      }
      
      # Set environment variables directly
      env {
        S3_BUCKET = "fimc-data"
        REGISTRY_TOKEN = "${NOMAD_META_registry_token}"
        # AWS credentials will be set if provided, otherwise they'll be empty
        # which will allow boto3 to fall back to using IAM roles
        AWS_ACCESS_KEY_ID = "${NOMAD_META_aws_access_key}"
        AWS_SECRET_ACCESS_KEY = "${NOMAD_META_aws_secret_key}"
        AWS_SESSION_TOKEN = "${NOMAD_META_aws_session_token}"
      }
      
      resources {
        cpu    = 2000
        memory = 3500
      }
      logs {
        max_files     = 10
        max_file_size = 10
      }
    }
  }
}
