# python-chart-airgab

Purpose
- This tool reads a single repo-level values.yaml that defines multiple addons (Helm charts), then:
  1) Resolves each chart version (supports standard Helm repos and OCI registries like public.ecr.aws and ghcr.io)
  2) Downloads and extracts the chart
  3) Renders the chart to discover container images (via helm template + yq)
  4) Pulls those images locally (with platform-aware logic) and pushes them to your private ECR (or a registry you specify)
  5) Pushes the Helm chart tarball to your ECR as an OCI artifact
  6) Writes a values.yaml snippet that maps image repositories/tags to your private ECR
  7) Logs a dependency graph for each chart (declared + vendored subcharts)
  8) Tags any ECR repositories created by the script with chart-syncer=true
  9) Handles Windows credential helper issues by sandboxing Docker and Helm registry config

Typical Use Case
- Move all images and charts referenced by a large “addons” values.yaml into a private, air‑gapped registry so they can be re-used without external downloads.

What gets created
- ECR chart repositories (flattened):
  - oci://<registry>/<chart>:<chart_version> (chart pushed under repo named exactly <chart>)
- ECR image repositories:
  - <registry>[/prefix]/<chart>/<image_name>:<tag>
- Local output:
  - ./helm-charts/<chart>/<chart>-<version>.tgz and extracted chart files
  - ./helm-charts/<chart>/values.yaml (image override snippet for private ECR)

How it Works (high level)
- Discovers addons from ./values.yaml (or a path you supply). It supports:
  - A top-level “addons” key (list or map) with fields like chart, repository (or repoUrl), targetRevision (version), release name, optional oci_namespace
  - Heuristic discovery for objects containing chart + repository/repoUrl
- For each addon:
  - Resolves chart version (—latest or pinned targetRevision)
  - Downloads chart and logs dependency graph (Chart.yaml + charts/ tree)
  - Renders chart (optionally with dependencies) and extracts image strings using yq
  - Pulls each image (platform-aware), tags, and pushes to ECR
  - Pushes chart tgz to ECR as OCI (flattened chart repo path)
  - Writes overrides ./helm-charts/<chart>/values.yaml (maps public image -> private repo/tag)

Authentication & Sandboxing (Windows-safe)
- Docker sandbox:
  - All docker commands run with DOCKER_CONFIG set to .docker-sandbox/<addon> and a minimal config.json ({"credsStore": ""}) to bypass OS credential helpers (“The stub received bad data” on Windows).
  - Public ECR docker login uses the current AWS identity by default via:
    aws ecr-public get-login-password | docker login --username AWS --password-stdin public.ecr.aws
- Helm sandbox (no env vars required):
  - All helm registry/repo operations run with CLI flags pointing to sandboxed files in .helm-sandbox/<addon>:
    --registry-config .helm-sandbox/<addon>/registry.json
    --repository-config .helm-sandbox/<addon>/repositories.yaml
    --repository-cache .helm-sandbox/<addon>/cache
  - OCI features enabled for registry/dependency operations (HELM_EXPERIMENTAL_OCI=1).
  - Public/private ECR helm registry logins use the current AWS identity by default.
- Docker Hub support (optional):
  - If you provide --dockerhub-username/--dockerhub-token (or env vars), the tool will docker login to docker.io within the sandbox before pulling docker hub images, raising rate limits and allowing private pulls.

Platform-aware Image Pulls
- CLI flag --platform with values:
  - auto (default): pull without platform → linux/amd64 → linux/arm64 until a concrete platform is detected
  - linux/amd64 or linux/arm64: pull explicitly with that platform
- After each pull, the image is inspected (docker inspect --format '{{.Os}}/{{.Architecture}}') and a pull without a concrete platform is treated as a failure and retried with the next platform option.
- Before pushing, the source tag is re‑inspected; if missing platform metadata, the image is re‑pulled with platform logic to ensure a single-arch image is pushed to ECR.

Dependency & Template Robustness
- Dependencies:
  - helm dependency build (sandboxed) to vendor subcharts
  - If build fails, helm dependency update then build
  - If still failing, remove Chart.lock and charts/ dir, then update+build again (sandboxed)
  - For OCI dependencies on public.ecr.aws, the tool logs into public ECR before dependency operations
- Template required values:
  - Some charts require minimal values to render for image extraction. The tool pre‑injects minimal safe overrides:
    - aws-load-balancer-controller:
      --set-string clusterName=placeholder
    - karpenter:
      --set-string settings.clusterName=placeholder
      --set-string settings.clusterEndpoint=https://placeholder
  - A retry with these overrides is also used if the first attempt fails.

Repository Naming
- Chart repository path (flattened):
  - Chart stored as <registry>/<chart>:<version> (no prefix)
- Image repository path:
  - <registry>/<prefix?>/<chart>/<image_name>:<tag>
- Optional “prefix” applies only to image repositories by default; chart repos are intentionally flattened under <chart>.

Requirements
- Python 3.8+
- Helm 3
- yq (Mike Farah) in PATH
- Docker
- AWS CLI v2
- AWS credentials with ECR permissions (describe/create/tag/push/list)
- Windows users: ensure helm.exe, yq.exe, docker, aws are on PATH

Install
```
pip install -r requirements.txt
```

Quick Start
- Process all addons in values.yaml and push images/charts to your current AWS account/region ECR:
```
python main.py --values ./values.yaml --latest --push-images --include-dependencies
```

- Push to a specific registry and path prefix for images:
```
python main.py --values ./values.yaml --push-images \
  --target-registry 123456789012.dkr.ecr.us-east-1.amazonaws.com \
  --target-prefix team/x
```

- Dry run (extract images + write overrides, but do not push):
```
python main.py --values ./values.yaml --scan-only
```

- Control dependency rendering:
```
python main.py --values ./values.yaml --include-dependencies   # default
python main.py --values ./values.yaml --exclude-dependencies
```

- Force platform for image pulls (e.g., amd64):
```
python main.py --values ./values.yaml --push-images --platform linux/amd64
```

- Use Docker Hub credentials (raises rate limits & enables private pulls):
```
python main.py --values ./values.yaml --push-images \
  --dockerhub-username "<YOUR_USER>" --dockerhub-token "<YOUR_TOKEN>"
# or with env:
# set DOCKERHUB_USERNAME=<YOUR_USER>
# set DOCKERHUB_TOKEN=<YOUR_TOKEN>
# python main.py --values ./values.yaml --push-images
```

- AppSet mode (legacy single-appspec flow):
```
python main.py --appspec my-appspec-name --push-images
```

CLI Arguments (detailed)
- --appspec <name>
  Use legacy “ApplicationSet” mode. Scans ./application-sets for YAMLs containing <name>, extracts one addon spec from each, and processes it. If provided, values mode is ignored.
- --values <path> (default: ./values.yaml)
  Path to an addons values.yaml containing multiple charts. The tool discovers addons by reading known keys (chart, repository/repoUrl, targetRevision) or heuristics.
- --latest
  Prefer the latest chart version when resolving. If a chart version is omitted in values.yaml, latest is applied automatically for that chart.
- --scan-only
  Do not push images/charts. Useful to test render/extraction and generate override values without writing to ECR.
- --push-images
  Push images (and chart) to the target registry. If neither --scan-only nor --push-images is provided, the tool will push by default.
- --target-registry <REGISTRY>
  Override the destination registry root (default: your AWS account/region ECR discovered via STS and AWS config).
  Example: 123456789012.dkr.ecr.us-east-1.amazonaws.com
- --target-prefix <PREFIX>
  Optional path prefix for image repos under the registry root.
  Example: team/x → <registry>/team/x/<chart>/<image_name>:<tag>
- --include-dependencies / --exclude-dependencies
  Whether to include subcharts when templating to extract images (default: include). Include is recommended for charts that reference images in subcharts (e.g., argo-cd + redis-ha).
- --platform {auto|linux/amd64|linux/arm64} (default: auto)
  Platform preference for image pulls. Auto tries none → amd64 → arm64, validating a concrete platform after each pull.
- --public-ecr-password / env ECR_PUBLIC_PASSWORD
  Override token/password for public.ecr.aws. If provided, used for both docker login and helm registry login. If not provided, the tool uses current AWS identity via aws ecr-public get-login-password.
- --private-ecr-password / env ECR_PRIVATE_PASSWORD
  Override token/password for your target private ECR. If provided, used for both docker login and helm registry login. If not provided, the tool uses current AWS identity via aws ecr get-login-password.
- --dockerhub-username / env DOCKERHUB_USERNAME
  Docker Hub username for authenticated pulls (optional).
- --dockerhub-token / env DOCKERHUB_TOKEN
  Docker Hub access token (or password) for the username (optional).

Environment Variables (optional)
- ECR_PUBLIC_PASSWORD
  Public ECR token override; otherwise current AWS identity is used.
- ECR_PRIVATE_PASSWORD
  Private ECR token override; otherwise current AWS identity is used.
- DOCKERHUB_USERNAME, DOCKERHUB_TOKEN
  Docker Hub credentials used for docker login to docker.io (sandboxed), improving rate limits and enabling private pulls.

Outputs per chart
- Chart OCI pushed to:
  - oci://<registry>/<chart>:<chart_version> (flattened chart repo path)
- Images pushed to:
  - <registry>/<prefix?>/<chart>/<image_name>:<tag>
- Image overrides values snippet written to:
  - ./helm-charts/<chart>/values.yaml
  - Maps standard image fields (repository/tag pairs) to the new private ECR locations.

Troubleshooting
- Windows “The stub received bad data” when talking to ECR:
  - All docker and helm auth are sandboxed to bypass OS credential helpers. If you still have issues, ensure no interactive prompts are blocking (timeouts are in place).
- Public ECR rate limits:
  - The tool always logs in to public ECR using the current AWS identity for higher quotas. Retries/backoff are used when transient errors occur.
- “Image does not provide any platform”:
  - Use --platform linux/amd64 (or arm64) if your environment is known. Auto mode retries with explicit platforms and verifies the resulting image metadata before push.
- Chart.lock out of sync with Chart.yaml:
  - The tool runs dependency update/build in a Helm sandbox. If necessary, it removes Chart.lock and charts/ and rebuilds dependencies.
- “name unknown” or 404 on helm push:
  - Chart pushes are flattened to repository named exactly after the chart (no prefix). This matches ECR behavior with helm push to oci://<registry>.

Support checklist for new engineers
- Confirm prerequisites installed and on PATH (helm, yq, docker, aws)
- Confirm AWS credentials for the target account/region
- Decide: use default account ECR or set --target-registry and optionally --target-prefix
- Decide dependency handling (—include-dependencies is recommended initially)
- (Optional) Provide Docker Hub credentials to raise pull limits
- Run:
```
python main.py --values ./values.yaml --latest --push-images --include-dependencies --platform auto
```
- Verify in ECR:
  - Chart repository: <registry>/<chart>
  - Image repos: <registry>/<prefix?>/<chart>/<image_name>
  - Tag chart repos created by the tool should have chart-syncer=true (applied automatically on creation)

Examples
- Push everything to default account/region ECR (auto platform, include dependencies):
```
python main.py --values ./values.yaml --latest --push-images --include-dependencies
```
- Force amd64 image pulls and use Docker Hub credentials:
```
python main.py --values ./values.yaml --push-images --platform linux/amd64 \
  --dockerhub-username "<YOUR_USER>" --dockerhub-token "<YOUR_TOKEN>"
```
- Target a specific registry and prefix for image repositories:
```
python main.py --values ./values.yaml --push-images \
  --target-registry 123456789012.dkr.ecr.us-east-1.amazonaws.com \
  --target-prefix platform/eks
```
- Scan-only (no pushes), write overrides for inspection:
```
python main.py --values ./values.yaml --scan-only --include-dependencies
```
- AppSet mode (legacy):
```
python main.py --appspec my-app --push-images
