# Cloud Agent

Cloud Agent is a self-hosted coding workspace for GitHub repositories. Run the same image as an
Azure Container App (web UI) and an event-driven Container Apps Job (worker). A signed-in user
selects an allow-listed repository, opens a session, and sends a coding request. Each turn runs in
one disposable job with a fresh clone; its conversation is kept in Azure Blob Storage and its code
is kept on a session-specific Git branch and pull request. Later turns recover both.

The default coding harness calls **only the HTTPS chat-completions endpoint you configure**. It
uses a self-hosted model or Microsoft Foundry deployment, never DeepSeek's hosted API. The harness
can inspect, search, edit, delete files, and run allow-listed validation commands. An optional
Copilot CLI adapter remains available through the common `CodingHarness` interface. You can also
select the pinned upstream [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
headless CLI with `HARNESS_PROVIDER=dsh`; it uses the same private model configuration. Its
hosted DeepSeek adapter, web routes, and telemetry are explicitly disabled. The built-in
`deepseek` adapter stays the default because it supports both Bearer and `api-key` model auth and
enforces a smaller coding tool surface.

## Release image

Tags `v0.x.y` run offline tests and publish `ghcr.io/a-hoier/cloud-agent:0.x.y` and
`ghcr.io/a-hoier/cloud-agent:0.x`. Pushes to `main` publish `:edge`. Pin a full version or digest
for deployments. [The publishing workflow](.github/workflows/publish.yml) uses GitHub's
`GITHUB_TOKEN`; no publishing PAT is required. Set the GHCR package visibility to public if you
want anonymous pulls. A private package needs an ACA registry credential.

To publish a release, update the version in `pyproject.toml` and `CHANGELOG.md`, merge to `main`,
then push an annotated matching tag such as `v0.1.0`. The workflow rejects a tag whose version
does not match `pyproject.toml`.

The optional `deploy` job updates an existing Container App named `cloud-agent-web` and an event
job named `cloud-agent-worker` after a successful `main` image build. It deploys the immutable
published image digest, so the app can develop itself after a reviewed change merges. Configure
repository Actions variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and
`AZURE_RESOURCE_GROUP` to enable it. The Azure identity must trust the repository's `main` branch
through GitHub OIDC and have Container Apps Contributor on the web app plus Container Apps Jobs
Contributor on the worker job. Without these variables, the Azure deployment step is skipped.

## What you need

- An Azure Storage account with queue `cloud-agent-tasks` and Blob container `cloud-agent`.
- A user-assigned managed identity (or separate identities for web and worker) with data-plane
  access to those resources. The web needs **Storage Queue Data Message Sender** and **Storage Blob
  Data Contributor**. The worker needs **Storage Queue Data Message Processor**, **Storage Queue
  Data Reader** (for scaling), and **Storage Blob Data Contributor**.
- An allow-list of GitHub repositories, a fine-grained PAT with **Contents: read/write** and
  **Pull requests: read/write** on those repositories, and a self-hosted model endpoint that
  supports OpenAI-style chat completions with tool calls.
- Microsoft Entra authentication on the web Container App, configured to **require sign-in** for
  all requests. Cloud Agent uses ACA's trusted `X-MS-CLIENT-PRINCIPAL-ID` header to bind sessions
  and task results to their creator. Do not put the web image behind another proxy that allows
  callers to forge this header.

Create the queue and container ahead of time; Cloud Agent does not create infrastructure. For a
user-assigned identity, set `AZURE_CLIENT_ID` to its client ID on both roles. `DefaultAzureCredential`
then obtains Storage and, when applicable, Foundry tokens. The worker identity also needs the
appropriate Foundry model-inference role (typically **Cognitive Services OpenAI User** or the
equivalent role for the chosen Foundry endpoint).

## Environment

These are the only required application settings for a GitHub + Foundry deployment:

| Setting | Role | Meaning |
| --- | --- | --- |
| `AZURE_STORAGE_ACCOUNT_NAME` | both | Derives Queue and Blob URLs; no connection string |
| `AZURE_CLIENT_ID` | both, if user-assigned identity | Managed identity client ID |
| `GITHUB_REPOSITORIES` | both | Comma-separated `owner/repo` allow-list, e.g. `acme/api,acme/web@develop` |
| `GITHUB_PAT` | worker only | Fine-grained token for clone, branch push, and PR |
| `MODEL_ENDPOINT` | worker only | Full HTTPS `/responses` or `/chat/completions` URL |
| `MODEL_NAME` | worker only | Deployment/model ID accepted by the endpoint |
| `MODEL_API_KEY` | worker, optional | Model API key; if absent, use managed identity for a Foundry endpoint |

Default Queue name is `cloud-agent-tasks`; default Blob container is `cloud-agent`. Change them
with `QUEUE_NAME`, `QUEUE_ACCOUNT_URL`, or `TASK_STATUS_CONTAINER_URL` if necessary.

`MODEL_ENDPOINT` examples:

```text
https://models.internal.example/v1/chat/completions
https://af-ahoier-pg.cognitiveservices.azure.com/openai/v1/responses
https://my-resource.openai.azure.com/openai/v1/chat/completions
https://my-resource.services.ai.azure.com/api/projects/my-project/openai/v1/chat/completions
```

With a key, `MODEL_AUTH_MODE=auto` (default) sends `api-key` to Azure Foundry hosts and Bearer
elsewhere; use `api-key` or `bearer` to override. Without a key, only Microsoft Foundry hosts are
accepted, and the worker gets a new Entra token before each model request. The default token scope
is `https://ai.azure.com/.default` for a Foundry project endpoint and
`https://cognitiveservices.azure.com/.default` for a resource endpoint; override with
`MODEL_TOKEN_SCOPE` if your deployment requires another scope. The web app never needs a model key
or GitHub PAT. Store secrets as ACA secrets and reference them by name, not literal command-line
values.

For Responses API models with reasoning and function tools, set `MODEL_REASONING_EFFORT=medium`.
The built-in harness sends this as `reasoning.effort` and uses response IDs to continue tool calls.
Chat Completions endpoints remain supported and receive `reasoning_effort` instead; some models
only support function calling there with `none`. The setting is omitted if unset. The `dsh`
provider currently requires a Chat Completions endpoint; use the default `deepseek` provider for
Responses API.

For customized repository names, default branches, source paths, or guidance, provide a JSON
allow-list with `REPOSITORY_REGISTRY_PATH` or `REPOSITORY_REGISTRY_BLOB_URL` instead of
`GITHUB_REPOSITORIES`. See [the example](config/repositories.json). A plain `GITHUB_REPOSITORIES`
entry uses `main` as its base branch unless it has an `@branch` suffix; the user can also select a different branch when opening a
session. The frontend never accepts arbitrary clone URLs.

Optional settings: `PUSH_ENABLED=false` for a trial without remote writes;
`HARNESS_PROVIDER=dsh` for the upstream headless CLI (Bearer model auth only), or
`HARNESS_PROVIDER=copilot` and a separate `GH_TOKEN` for Copilot CLI;
`DEEPSEEK_MAX_STEPS=40`, `DEEPSEEK_TIMEOUT_SECONDS=1200`, and
`QUEUE_VISIBILITY_TIMEOUT=2100`. `QUEUE_MAX_MESSAGES` is fixed at one so a worker handles only one
turn. A no-change turn creates no branch or commit.

## Deploy to Azure Container Apps

Create an ACA environment, Storage resources, identity, and role assignments first. Then deploy
the release image twice. The following commands show the important shape; replace placeholders
and use ACA secret references for the PAT and optional model key:

```bash
az containerapp create \
  --name cloud-agent-web --resource-group <rg> --environment <environment> \
  --image ghcr.io/a-hoier/cloud-agent:0.1.1 \
  --user-assigned <identity-resource-id> \
  --ingress external --target-port 8000 --command cloud-agent-web \
  --env-vars AZURE_STORAGE_ACCOUNT_NAME=<account> AZURE_CLIENT_ID=<identity-client-id> \
    GITHUB_REPOSITORIES=acme/api,acme/web
```

**Before sharing the URL**, enable ACA built-in Microsoft Entra authentication and set unauthenticated
requests to **require authentication**. The API rejects requests without a principal ID; ACA
strips client-supplied identity headers. Merely making the app private is not a substitute for
this setting. Restrict which users can sign in through your Entra app assignment/policy.

```bash
az containerapp job create \
  --name cloud-agent-worker --resource-group <rg> --environment <environment> \
  --image ghcr.io/a-hoier/cloud-agent:0.1.1 \
  --mi-user-assigned <identity-resource-id> \
  --trigger-type Event --replica-timeout 1800 --replica-retry-limit 0 \
  --parallelism 1 --min-executions 0 --max-executions 5 --polling-interval 30 \
  --scale-rule-name cloud-agent-tasks --scale-rule-type azure-queue \
  --scale-rule-metadata accountName=<account> queueName=cloud-agent-tasks queueLength=1 \
  --scale-rule-identity <identity-resource-id> \
  --cpu 2 --memory 4Gi --command cloud-agent-worker \
  --env-vars AZURE_STORAGE_ACCOUNT_NAME=<account> AZURE_CLIENT_ID=<identity-client-id> \
    GITHUB_REPOSITORIES=acme/api,acme/web MODEL_ENDPOINT=<chat-completions-url> \
    MODEL_NAME=<deployment-id> GITHUB_PAT=secretref:github-pat
```

Add `MODEL_API_KEY=secretref:model-api-key` only when needed. Keep the queue visibility timeout
longer than the job replica timeout to avoid parallel processing of the same turn. The queue
handles retries; the ACA job retry limit should stay at zero. A job exits `0` for a handled turn,
`1` for a task failure, and `2` for startup/configuration failure.

## Security model

Sessions are stored under a per-owner Blob prefix derived from the ACA principal ID. The API
checks ownership on reads and writes and returns 404 for another user's IDs. The worker verifies
that a queued turn matches its session's owner, repository, and instruction. All authenticated
users can choose **any** repository on the deployment allow-list, because the worker has one
shared PAT. For different repository access groups, use separate deployments/credentials or add
an authorization gateway. A session URL is not a sharing permission.

The worker does not pass the GitHub PAT to the model request or test subprocess. Git credentials
are scoped to `github.com` in Git's environment, never placed in a clone URL or argv, and Git
errors are redacted. The agent is instructed not to change Git history; the wrapper owns commit,
push, and PR creation. This is **not** a hostile-code sandbox: repository tests and build scripts
run inside the worker container, which has model and Git credentials in its process environment.
Only attach repositories and users you trust with that worker identity. For mutually untrusted
tenants, deploy separate workers with distinct identities/tokens and network policies.

## Develop locally

```bash
python -m pip install -e '.[dev]'
python -m pytest -m 'not integration'
ruff check .
docker build -t cloud-agent:dev .
```

See [.env.example](.env.example) for the local configuration shape. Start the web process with
`cloud-agent-web` and a worker with `cloud-agent-worker`. Local API calls need ACA's trusted
identity header, so use an authenticated ACA deployment for end-to-end testing. Integration tests
are opt-in via `AIOPS_INTEGRATION=1` and `AIOPS_IT_REPOSITORY=<id>`; queue writes and Git pushes
have separate opt-in switches in [the tests](tests/integration/conftest.py).

The project is [MIT licensed](LICENSE) and at version 0.x. Expect breaking changes before 1.0;
pin release tags and review [the changelog](CHANGELOG.md) before upgrading.
