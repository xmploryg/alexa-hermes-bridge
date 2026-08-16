# Alexa-Hermes Bridge

A lightweight FastAPI service that lets an Alexa Custom Skill talk to your
Hermes Agent instance, with async handoff for anything that can't finish
within Alexa's ~8-second response window.

## Architecture

```
Alexa Custom Skill (Lambda / HTTPS endpoint)
        │  HTTPS
        ▼
alexa-bridge (FastAPI, k8s namespace "alexa-bridge")
        │  Bearer token (HERMES_API_KEY)
        ▼
Hermes Agent API Server on Unraid (192.168.1.17:8642)
   POST /v1/chat/completions
   header: X-Hermes-Session-Key: alexa:<amazon_user_id>
```

Alexa never talks to Hermes directly — the bridge handles the fast-path /
async-path decision, per-user session scoping, and speech-response shaping.

### The 8-second problem

Alexa expects a response in ~8 seconds. The bridge calls Hermes with a
`FAST_PATH_TIMEOUT_SECONDS` budget (default 6s):

- **Hermes answers in time** → the bridge speaks the reply verbatim.
- **Hermes is still working** → the bridge tells Alexa *"I'm working on
  that"* and detaches, letting the Hermes turn run to completion in the
  background. Hermes's own safety policy (approval gates, confirmation
  prompts) still applies exactly as it would on any other channel — the
  bridge does not pre-authorize or bypass anything. The full result / any
  approval prompt surfaces on the user's normal Hermes channel
  (Telegram/Discord), scoped by the same `X-Hermes-Session-Key`.

## Prerequisites

- Hermes Agent's OpenAI-compatible API server enabled (`platforms.api_server`
  in Hermes's `config.yaml`, or `API_SERVER_ENABLED=true` in `.env`), with
  `API_SERVER_KEY` set. See the Hermes docs/skill for exact config keys.
- Hermes reachable from the k3s cluster over the LAN
  (`http://192.168.1.17:8642`) — already true per existing cross-VLAN
  routing.
- A container registry to push the image to (this repo ships a GitHub
  Actions workflow that pushes to both Docker Hub and GHCR — add
  `.github/workflows/docker-publish.yml` manually; see below).
- Cloudflare Tunnel or public DNS + Traefik ingress, same as other
  services in `phoenixlab` (this repo mirrors `chatgpt-overseerr-bridge`).
- `cert-manager` + `letsencrypt-production` ClusterIssuer already deployed.

## Build and push the image

The CI workflow file couldn't be pushed automatically (GitHub blocks the
`workflow` scope for the bot token used here). Copy the content from
`chatgptoverseerrbridge/.github/workflows/docker-publish.yml` in this org,
replacing `IMAGE_NAME: chatgpt-overseerr-bridge` with
`IMAGE_NAME: alexa-hermes-bridge`, and commit it to
`.github/workflows/docker-publish.yml` here.

### One-time GitHub repo setup

In this repo → **Settings → Secrets and variables → Actions**, add:

| Type | Name | Value |
|------|------|-------|
| Secret | `DOCKERHUB_TOKEN` | Docker Hub access token |
| Variable | `DOCKERHUB_USERNAME` | Your Docker Hub username |
| Variable | `GHCR_USERNAME` | Your GitHub username |

The workflow runs automatically on the next matching push once added.

### Manual build (until CI is wired up)

```bash
git clone https://github.com/xmploryg/alexa-hermes-bridge.git
cd alexa-hermes-bridge
docker build -t ghcr.io/xmploryg/alexa-hermes-bridge:latest .
docker push ghcr.io/xmploryg/alexa-hermes-bridge:latest
```

## Deploy

### 1. Locate Hermes's API server key

This is `API_SERVER_KEY` from Hermes's `.env` on the Unraid host. Do not
commit it — it goes straight into a k8s Secret.

### 2. Create the namespace and secret

```bash
kubectl apply -f k8s/namespace.yaml

kubectl -n alexa-bridge create secret generic bridge-secrets \
  --from-literal=HERMES_API_KEY=<value-of-API_SERVER_KEY>
```

(`k8s/secret.yaml` is a template only — never commit real values to it.)

### 3. Apply remaining manifests

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

### 4. Verify

```bash
kubectl -n alexa-bridge rollout status deploy/alexa-bridge

curl https://alexa-bridge.phoenixlab.me/health
# → {"status":"ok"}
```

## Expose publicly for Alexa

Alexa needs an HTTPS URL reachable from the internet.

### Cloudflare Tunnel (recommended, already deployed in this cluster)

In the Cloudflare Zero Trust dashboard, add a public hostname:

| Field | Value |
|-------|-------|
| Subdomain | `alexa-bridge` |
| Domain | `phoenixlab.me` |
| Service | `http://alexa-bridge.alexa-bridge.svc.cluster.local:8000` |

This gives `https://alexa-bridge.phoenixlab.me` with no exposed ports.

## Configure the Alexa Custom Skill

In the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask):

1. Create a Custom Skill, invocation name of your choice (2+ words for
   eventual public certification).
2. **Endpoint**: HTTPS, pointed at `https://alexa-bridge.phoenixlab.me/alexa`.
   Use a certificate with a valid CN (Let's Encrypt via cert-manager works).
3. **Interaction model**: one custom intent (e.g. `HermesQueryIntent`) with
   a single required slot `Query` of type `AMAZON.SearchQuery`, plus sample
   utterances like:
   - `ask hermes {Query}`
   - `to {Query}`
   - `{Query}`
4. Set `ALEXA_SKILL_ID` in `k8s/configmap.yaml` to the skill's
   `amzn1.ask.skill.*` application ID once created, and re-apply, so the
   bridge rejects requests claiming to be any other skill.
5. **Note on signature verification**: this bridge currently checks only
   the `applicationId` in the request body as defense-in-depth. Before any
   public/production exposure, add full Alexa request signature
   verification (SignatureCertChainUrl + timestamp tolerance) per Amazon's
   skill security docs.

## Files

```
alexa-hermes-bridge/
├── app/
│   └── main.py              # FastAPI bridge app
├── requirements.txt
├── Dockerfile
├── k8s/
│   ├── namespace.yaml
│   ├── secret.yaml          # template only — do not commit real values
│   ├── configmap.yaml       # HERMES_API_URL, timeouts, skill id
│   ├── deployment.yaml
│   ├── service.yaml
│   └── ingress.yaml         # public HTTPS via Traefik + cert-manager
└── README.md
```
