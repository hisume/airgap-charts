# python-chart-airgab

Purpose
- This tool reads a single repo-level values.yaml that defines multiple addons (Helm charts), then:
  1) Resolves each chart version (supports standard Helm repos and OCI registries like public.ecr.aws and ghcr.io)
  2) Downloads and extracts the chart
  3) Renders the chart to discover container images (via helm template + yq)
  4) Pulls those images locally and pushes them to your private ECR (or a registry you specify)
  5) Pushes the Helm chart tarball to your ECR as an OCI artifact
  6) Writes a values.yaml snippet that maps image repositories/tags to your private ECR
  7) Logs a dependency graph for each chart (declared + vendored subcharts)
  8) Tags any ECR repositories created by the script with chart-syncer=true

Typical Use Case
- Move all images and charts referenced by a large “addons” values.yaml into a private, air-gapped registry so they can be re-used without external downloads.

What gets created
- ECR chart repositories:
  - <registry>[/prefix]/<chart>:<chart_version> (stored as OCI artifact)
- ECR image repositories:
  - <registry>[/prefix]/<chart>/<image_name>:<tag>
- Local output:
  - ./helm-charts/<chart>/<chart>-<version>.tgz and extracted chart files
  - ./helm-charts/<chart>/values.yaml (image override snippet for private ECR)


How it Works (high level)
- Discovers addons from ./values.yaml (or a path you supply). It supports:
  - A top-level “addons” key (list or map) with fields like chart, repoUrl, targetRevision
  - Heuristic discovery for objects containing chart + repoUrl
- For each addon:
  - Resolves chart version (—latest or pinned targetRevision)
  - Downloads chart and logs dependency graph (Chart.yaml + charts/ tree)
  - Renders chart (optionally with dependencies) and extracts image strings using yq
  - Pulls each image, tags, and pushes to ECR
  - Pushes chart tgz to ECR as OCI
  - Writes overrides ./helm-charts/<chart>/values.yaml (maps public image->private repo/tag)

Notes on repository naming
- Chart repository path: <registry>/<prefix?>/<chart>
- Image repository path: <registry>/<prefix?>/<chart>/<image_name>:<tag>
- It is normal that the chart repo (e.g., 1234.dkr.ecr.us-east-1.amazonaws.com/argo-cd:9.0.1) differs from image repos (e.g., 1234.dkr.ecr.us-east-1.amazonaws.com/argo-cd/argocd:v3.1.9). Chart and images are separate artifacts with different version semantics.

Requirements
- Python 3.8+
- Helm 3
- yq (Mike Farah) in PATH
- Docker
- AWS CLI v2
- AWS credentials with ECR permissions (describe/create/tag/push/list)
- Windows users: ensure helm.exe and yq.exe are in PATH

Install
pip install -r requirements.txt

Quick Start
- Process all addons in values.yaml and push images/charts to your current AWS account/region ECR:
  python main.py --values ./values.yaml --latest --push-images
- Push to a specific registry and path prefix:
  python main.py --values ./values.yaml --push-images --target-registry 123456789012.dkr.ecr.us-east-1.amazonaws.com --target-prefix team/x
- Dry run (extract images + write overrides, but do not push):
  python main.py --values ./values.yaml --scan-only
- Control dependency rendering (some charts need this to expose images, e.g., argo-cd):
  python main.py --values ./values.yaml --include-dependencies
  python main.py --values ./values.yaml --exclude-dependencies

CLI Arguments (detailed)
- --appspec <name>
  Use legacy “ApplicationSet” mode. Scans ./application-sets for YAMLs containing <name>, extracts one addon spec from each, and processes it. If provided, values mode is ignored.
- --values <path> (default: ./values.yaml)
  Path to an addons values.yaml containing multiple charts. The tool discovers addons by reading known keys (chart, repoUrl, targetRevision) or heuristics.
- --latest
  If set, when resolving chart version, prefer the latest available. If no version is specified in values.yaml, latest is applied automatically.
- --scan-only
  Do not push images/charts (no vulnerability scanning is performed). Useful to test render/extraction and generate override values without writing to ECR.
- --push-images
  Push images (and chart) to the target registry. If neither --scan-only nor --push-images is provided, the tool will push by default.
- --target-registry <REGISTRY>
  Override the destination registry root (default: your AWS account/region ECR discovered via STS and AWS config).
  Example: 123456789012.dkr.ecr.us-east-1.amazonaws.com
- --target-prefix <PREFIX>
  Optional path prefix for repos under the registry root. Example: team/x creates repos like:
  1234.dkr.ecr.us-east-1.amazonaws.com/team/x/<chart> and 1234.dkr.ecr.us-east-1.amazonaws.com/team/x/<chart>/<image_name>
- --include-dependencies / --exclude-dependencies
  Whether to include subcharts when templating to extract images (default: include). Include is recommended for charts that reference images in subcharts (e.g., argo-cd + redis-ha).
- --public-ecr-password / env ECR_PUBLIC_PASSWORD
  Override token/password for public.ecr.aws. If provided, used for both docker login and helm registry login. If not provided, the tool uses aws ecr-public get-login-password.
- --private-ecr-password / env ECR_PRIVATE_PASSWORD
  Override token/password for your target private ECR. If provided, used for both docker login and helm registry login. If not provided, the tool uses aws ecr get-login-password.

Behavior Details
- Dependency graph logging:
  - Declared dependencies: read from Chart.yaml (name, repository, version, alias/condition if present)
  - Vendored dependencies: recursively scans charts/ in the extracted chart directory
- Image extraction:
  - Runs “helm template” (with dependencies enabled by default) and uses yq to find ..|.image? occurrences
  - Some charts require values to render (e.g., clusterName). If templating fails, images may be empty and only the chart gets pushed.
- ECR repository tagging:
  - Repositories created by this script are tagged on creation:
    chart-syncer=true
  - Existing repositories are not modified.

Outputs per chart
- Chart OCI pushed to:
  oci://<registry>/<prefix?> (repository: <chart>, tag: <chart_version>)
- Images pushed to:
  <registry>/<prefix?>/<chart>/<image_name>:<tag>
- Image overrides values snippet written to:
  ./helm-charts/<chart>/values.yaml
  This maps standard image fields (repository/tag pairs) to the new private ECR locations.

Troubleshooting
- Missing CLI tools:
  - The tool checks for helm and yq always; docker and aws when pushing. Install and ensure they are on PATH.
- Windows “The stub received bad data” when talking to public.ecr.aws:
  - Provide a token via --public-ecr-password or set ECR_PUBLIC_PASSWORD
  - Alternatively, ensure Docker credential helpers/stores are configured appropriately
- Templating schema errors (e.g., cert-manager):
  - Some charts reject arbitrary extra values; the tool avoids universal --set. If a chart requires specific values to render, provide them in values.yaml under the chart’s values.
- “Missing dependency chart” (e.g., redis-ha for argo-cd):
  - Use --include-dependencies (default) so the tool vendors subcharts (helm dependency build) and can render successfully.
- Images not found:
  - Some charts do not declare images in a form that can be easily parsed from templated output. Check the chart templates or the root values file for image keys.

FAQs
- Why is the chart repo different from the image repos?
  - Helm charts are pushed as OCI artifacts with their own version (e.g., 9.0.1 for argo-cd). Container images have independent tags (e.g., v3.1.9). They live in separate repositories:
    - Chart: <registry>/<prefix?>/argo-cd:9.0.1
    - Images: <registry>/<prefix?>/argo-cd/argocd:v3.1.9
- Does the tool download dependent charts?
  - When you include dependencies (default), it runs “helm dependency build” so templating can render subcharts. It does not permanently pull/store those subcharts outside the chart’s extraction folder.
- How are ECR repos named?
  - Images: <chart>/<image_name>
  - Charts: <chart>
  - Optional prefix is prepended to both.

Support checklist for new engineers
- Confirm prerequisites installed and on PATH (helm, yq, docker, aws)
- Confirm AWS credentials for the target account/region
- Decide: use default account ECR or set --target-registry and optionally --target-prefix
- Decide dependency handling (—include-dependencies is recommended initially)
- Run:
  python main.py --values ./values.yaml --latest --push-images
- Verify in ECR:
  - Chart repository: <registry>/<prefix?>/<chart>
  - Image repos: <registry>/<prefix?>/<chart>/<image_name>
  - Tag chart repos created by the tool should have chart-syncer=true (applied automatically on creation)
