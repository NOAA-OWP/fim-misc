# setup

## downloading flows2fim binary
The script and docker image need a flows2fim binary for the version of flows2fim you are generating extents for. The version is set in the Dockerfile when building the image. You can download at [the flows2fim github page](https://github.com/NGWPC/flows2fim/releases/)

## environment file

The coordinator script reads from a .env file at the top level of the repo. An example.env file is provided in the repo.

To avoid having your shell environment conflict with your .env file run the coordinator like: `env -i PATH=$PATH python src/coordinator.py`

The .env file contains the following security sensitive tokens:

- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- REGISTRY_TOKEN

The AWS access keys need to be able to access the bucket fimc-data if you are running jobs on a local Nomad dev cluster. 

If you aren't deploying your jobs locally then the AWS credentials still need to be included in the .env file so the coordinator script can use them to assess the processing state of the current ripple library directory on s3. However, the workers don't need the AWS credentials in this case. This is because the Nomad API's inside the NGWPC accounts should be configured so that the Nomad clients have IAM roles that can write to the fimc-data bucket. If you are running the coordinator to deploy to a NGWPC Nomad then run the coordinator like: `env -i PATH=$PATH python coordinator.py --use-iam`. This will avoid passing the .env files AWS creds to the nomad clients so that they can fall back onto their IAM permissions to write to the fimc-data bucket. 

The REGISTRY_TOKEN is a gitlab deploy token that is used by the Nomad clients to pull container images. Each NGWPC team should have their own token provided by the NGWPC infrastructure team. To obtain your teams gitlab registry token contact the infrastructure team.

If you are deploying to a nonlocal Nomad API then you also need to set these variables to get access to the API:

- NOMAD_TOKEN

The NOMAD_TOKEN is currently created by the AWS secrets manager of the NGWPC AWS account the API is deployed in. The NOMAD_ADDR is the address of the API.

# Local development

## Starting the dev server
navigate to the repository and enter: `sudo nomad agent -dev -config=local_nomad.hcl` 
note that it is necessary to run the dev cluster as sudo so that nomad can use the docker driver.

## Development workflow

Enter a development environment by using:
```
docker compose up -d
docker compose exec worker-dev bash
```

This will allow you to make changes to the coordinator and worker scripts. 

## Building local docker image and pushing to gitlab container registry

When you have made all the changes to your worker scripts and config rebuild the container and push to gitlab with: 

```
docker build --no-cache -t f2f-runner:v0 .
docker tag f2f-runner:v0 registry.sh.nextgenwaterprediction.com/ngwpc/fim-c/flows2fim_extents
docker push registry.sh.nextgenwaterprediction.com/ngwpc/fim-c/flows2fim_extents
```

The above code snippet should work if run from a NGWPC AWS workspace.

Note that you might need to run the docker commands as sudo depending on your system's setup.

**Note**: The docker image needs to be redeployed every time you make a change to the worker script or the config files are changed! The reason we rebuild the docker image after changes are made is because code and config files are built into the image for ease of deploying containers to nomad workers.

## Registering a job

To register the flows2fim runner job to the Nomad API you would use: `nomad job run f2f-runner.nomad` 

If you don't have the Nomad CLI installed or would prefer to use the Nomad API you can also register a job by importing the postman collection in this repository into postman and then using the "Register nomad job" request after setting the nomadUrl and nomadToken collectionwide variables. If you are using a local cluster then leave the nomadToken variable blank. 

## Dispatching a job

The coordinator takes care of dispatching jobs through the Nomad API.

A single job can be dispatched using the Nomad CLI like:
```
nomad job dispatch -meta dir_name="ble_08020203_LowerStFrancis" -meta flow_file="flows_10year.csv" -meta return_period="10" -meta aws_access_key="$AWS_ACCESS_KEY_ID" -meta aws_secret_key="$AWS_SECRET_ACCESS_KEY" f2f-runner
```

Make sure to have your AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY if the resource you are running the worker on doesn't have IAM access to the fimc-data bucket.

# Purging jobs

It is helpful to purge jobs in between batches.

If you do have the dispatch ids then purge each individual dispatch *before* purging the parent job.

```
for job in $(nomad job status f2f-runner | grep "dispatch-" | awk '{print $1}'); do
  echo "Purging $job"
  nomad job stop -purge $job
done
```

If you don't have the Nomad CLI installed or would prefer to use the Nomad API you can also purge jobs by importing the postman collection in this repository into postman and then using the "Purge f2f-runner child jobs" request after setting the nomadUrl and nomadToken collectionwide variables. If you are using a local cluster then leave the nomadToken variable blank. 

# Running on a non-local Nomad API

To run on a non-local Nomad API the steps are very similar to running locally. The main differences are:

- You don't have to start the server yourself
- You need to change the Nomad address in the coordinator config so the coordinator knows where to post the jobs to
- You need to obtain a NOMAD_TOKEN from the secrets manager and put it in the environment you start the coordinator from 

# encountered Nomad errors

## No nodes in pool

This error can happen if there are no non-server nodes in the pool. If you manually set the desired capacity in AWS of the autoscaling group to higher than 1 then the currenty AWS Test deployment should be able to start allocating jobs.

