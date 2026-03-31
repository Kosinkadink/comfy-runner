# Hosted Deployment Plan: RunPod Backend for comfy-runner

## Overview

Add hosted GPU deployment support to comfy-runner, starting with RunPod. The goal is to deploy ComfyUI instances to on-demand cloud GPUs using the same workflow as local/remote — same CLI, same server API, same standalone environment approach.

The command namespace is `hosted` (not "cloud") to avoid confusion with Comfy Org's own Comfy Cloud product.

## Architecture

### Core Insight

A RunPod pod running comfy-runner server is architecturally identical to an existing remote comfy-runner server (e.g., accessed via Tailscale). The `hosted` layer only manages the pod lifecycle (create/stop/terminate) and provides the URL. Everything else flows through the existing comfy-runner server API.

```
Local machine                          RunPod Pod (thin Docker image)
┌──────────────┐                      ┌─────────────────────────────────┐
│ comfy-runner  │ ── HTTPS/proxy ──→  │ comfy-runner server              │
│ (client)      │                      │   ├── init/start/stop/deploy    │
│               │                      │   ├── snapshot/nodes/status     │
│               │                      │   └── all existing API routes   │
└──────────────┘                      │                                 │
                                       │ Network Volume (/workspace)     │
                                       │   ├── standalone python env     │
                                       │   ├── ComfyUI/                  │
                                       │   ├── models/                   │
                                       │   └── .comfy-runner/            │
                                       └─────────────────────────────────┘
```

### Deployment Strategy: "Thin Image + Fat Volume" (Strategy A)

- **Docker image**: Minimal — Ubuntu + git + curl + system Python + comfy-runner. No PyTorch, no CUDA toolkit. ~1 GB.
- **Network volume** (`/workspace`): Standalone ComfyUI environment (bundled Python + torch + deps), ComfyUI clone, custom nodes, models, venv, outputs.
- **NVIDIA drivers**: Injected by RunPod host at runtime.
- The standalone release bundles its own Python interpreter + torch + CUDA runtime. The Docker image is just a shell to run it.

### Why Strategy A

- Mirrors exactly how comfy-runner works locally.
- All existing comfy-runner code (process.py, environment.py, shared_paths.py, nodes.py, snapshot.py, deploy) works unchanged on the volume.
- No Docker Hub account or image rebuilds needed for ComfyUI/node changes.
- Volume is fully self-contained and portable across pods.
- Pods are disposable — terminate and recreate with a different GPU, same volume, instant ready state.

### Volume Reuse

Network volumes exist independently of pods and can be reattached to new pods:

- **Terminate** pod → volume persists (pay only storage, $0 compute)
- **Create new pod** in same datacenter → attach same volume → everything intact
- Constraint: volume and pod must be in the same datacenter
- Constraint: one pod per volume at a time
- Constraint: volume attached at pod creation time only (no hot-swap)

## Model Management

Two patterns, both supported without special machinery:

### 1. CI / Persistent Models

Long-lived volume with a curated model collection that grows over time. Models added via:
- `cloud volume sync-models` (bulk S3 upload from local shared_dir)
- Downloading during a run (models persist on the volume for next time)
- Volume reused across many pod lifecycles

### 2. Ad-hoc / On-demand Models

Small or empty volume. ComfyUI downloads models on-demand at runtime (HuggingFace, Civitai, etc.). Volume may be disposable after the test.

Model pre-loading is **never required** — `cloud init` works with an empty volume.

### Parallel Instances

For running multiple cloud instances simultaneously (different GPUs, PRs, branches):
- Each instance = 1 pod + 1 network volume (independent)
- Each `cloud init` is independent, no fleet/batch orchestration
- Model duplication across volumes is the tradeoff for parallelism
- `cloud volume sync-models <target> --from <source>` copies models between volumes via S3 API
- Sequential testing can reuse the same volume (terminate pod, create new pod, same volume)

## Release Cache Volume (Optimization)

Cache standalone environment downloads on a RunPod network volume for fast pod initialization.

### Problem
Standalone environment downloads from GitHub are ~2-4 GB and can be slow (CDN, rate limits). Each new `init` re-downloads from GitHub.

### Solution
Maintain a small "release cache" volume per datacenter. When `init` runs on a pod:
1. Check cache volume via S3 API for the requested release
2. If found, download at NVMe speed (200-400 MB/s, ~10s for 4 GB)
3. If not found, download from GitHub, then upload to cache volume

### Cache Maintenance
- Auto-maintain last N releases (default: 3)
- When a new release is cached, evict the oldest if at capacity
- Cache volume is small (~10-12 GB for 3 releases)
- Can be shared read-only across all pods in the datacenter via S3 API

## RunPod API Surface

All operations use the RunPod REST API (`https://rest.runpod.io/v1/`):

| Operation | Endpoint | Notes |
|---|---|---|
| Create pod | `POST /pods` | gpuTypeIds, imageName, networkVolumeId, ports, env |
| List pods | `GET /pods` | |
| Get pod | `GET /pods/{id}` | Status, GPU info, port mappings |
| Start pod | `POST /pods/{id}/start` | Resume stopped pod |
| Stop pod | `POST /pods/{id}/stop` | Release GPU, keep volume |
| Terminate pod | `DELETE /pods/{id}` | Destroy pod |
| Create volume | `POST /networkvolumes` | name, size, dataCenterId (max 4 TB via API) |
| List volumes | `GET /networkvolumes` | |
| Get volume | `GET /networkvolumes/{id}` | |
| Delete volume | `DELETE /networkvolumes/{id}` | |
| Create template | `POST /templates` | Reusable pod configuration |

### S3-Compatible API (for model/cache sync)
- Endpoint: `https://s3api-{DATACENTER}.runpod.io/`
- Bucket = network volume ID
- Standard boto3/AWS CLI compatible
- Available in: EUR-IS-1, EU-RO-1, EU-CZ-1, US-KS-2
- Requires separate S3 API key (generated in RunPod console)
- File path mapping: S3 `s3://VOLUME_ID/models/file.safetensors` = Pod `/workspace/models/file.safetensors`

### Port Exposure
- HTTP proxy: `https://{POD_ID}-{PORT}.proxy.runpod.net` (automatic HTTPS, 100s Cloudflare timeout)
- TCP direct: public IP + mapped port (Secure Cloud only, no timeout limit)
- ComfyUI UI: expose port 8188/http
- comfy-runner server: expose port 9189/http
- SSH: expose port 22/tcp (for deploy operations)

### 100-Second Proxy Timeout
RunPod's HTTP proxy has a 100s Cloudflare timeout. ComfyUI workflows >100s need:
- WebSocket connections (ComfyUI uses these natively for progress)
- Async polling via `/api/history` (works fine)
- TCP port exposure for truly long connections
- This matches ComfyUI's existing architecture — the UI uses WebSocket, not long HTTP requests

## Storage Costs

| Volume Size | Monthly Cost | Use Case |
|---|---|---|
| 50 GB | $3.50/mo | Ad-hoc testing, few models |
| 100 GB | $7/mo | Small model collection |
| 500 GB | $35/mo | Moderate CI collection |
| 1 TB | $70/mo | Large collection |
| 2 TB | $120/mo | Comprehensive setup |

First 1 TB: $0.07/GB/month. Beyond 1 TB: $0.05/GB/month.
No data transfer fees (ingress/egress free).

## GPU Pricing (relevant tiers, on-demand, per hour)

| GPU | VRAM | RunPod Price | Good For |
|---|---|---|---|
| RTX 3090 | 24 GB | $0.22/hr | Budget testing |
| RTX 4090 | 24 GB | $0.34/hr | Fast inference, small models |
| L4 | 24 GB | $0.44/hr | Efficient inference |
| L40S | 48 GB | $0.79/hr | Large models, good value |
| A100 SXM 80GB | 80 GB | $1.39/hr | Large models, training |
| H100 SXM | 80 GB | $2.69/hr | Maximum performance |

## Config Schema

```json
{
  "hosted": {
    "runpod": {
      "api_key": "rpa_...",
      "s3_access_key": "...",
      "s3_secret_key": "...",
      "default_gpu": "NVIDIA L40S",
      "default_datacenter": "US-KS-2",
      "default_cloud_type": "SECURE",
      "cache_releases": 3,
      "volumes": {
        "main": { "id": "agv6w2qcg7", "datacenter": "US-KS-2", "size_gb": 500 },
        "cache": { "id": "bx8w3kq1m2", "datacenter": "US-KS-2", "size_gb": 15, "role": "release-cache" }
      }
    }
  },
  "installations": {
    "hosted-main": {
      "type": "hosted",
      "provider": "runpod",
      "pod_id": "xedezhzb9la3ye",
      "volume_name": "main",
      "gpu_type": "NVIDIA L40S",
      "datacenter": "US-KS-2",
      "status": "running",
      "comfy_url": "https://xedezhzb9la3ye-8188.proxy.runpod.net",
      "server_url": "https://xedezhzb9la3ye-9189.proxy.runpod.net",
      "created_at": "2026-03-30T12:00:00Z"
    }
  }
}
```

## CLI Commands

### Hosted Configuration (one-time setup)
```bash
comfy_runner.py hosted config set runpod.api_key rpa_...
comfy_runner.py hosted config set runpod.s3_access_key ...
comfy_runner.py hosted config set runpod.s3_secret_key ...
comfy_runner.py hosted config set runpod.default_gpu "NVIDIA L40S"
comfy_runner.py hosted config set runpod.default_datacenter US-KS-2
comfy_runner.py hosted config show
```

### Volume Management
```bash
# Create a named volume
comfy_runner.py hosted volume create --name main --size 500 --region US-KS-2

# List volumes
comfy_runner.py hosted volume list

# Sync models from local shared_dir to volume via S3
comfy_runner.py hosted volume sync-models main

# Sync models from one volume to another
comfy_runner.py hosted volume sync-models target-vol --from main

# Sync models from volume to local
comfy_runner.py hosted volume sync-models main --pull

# Delete a volume
comfy_runner.py hosted volume rm <name>
```

### Instance Lifecycle
```bash
# Create + start a hosted instance (with existing named volume)
comfy_runner.py hosted init --name hosted-main --gpu "NVIDIA L40S" --volume main
#  → Creates pod, attaches volume, runs comfy-runner server
#  → If volume is fresh, runs init (download standalone env, clone ComfyUI)
#  → Prints ComfyUI URL + server URL

# Create + start with a new ad-hoc volume
comfy_runner.py hosted init --name quick-test --gpu "NVIDIA RTX 4090" --volume-size 50

# Stop (release GPU, keep pod record + volume)
comfy_runner.py hosted stop hosted-main

# Start (resume stopped pod)
comfy_runner.py hosted start hosted-main

# Terminate (destroy pod, keep volume)
comfy_runner.py hosted terminate hosted-main

# Status
comfy_runner.py hosted status hosted-main

# Get URLs
comfy_runner.py hosted url hosted-main

# List all hosted instances
comfy_runner.py hosted list
```

### Deploy (via comfy-runner server API on the pod)
```bash
comfy_runner.py hosted deploy hosted-main --pr 1234
comfy_runner.py hosted deploy hosted-main --branch feature-x
comfy_runner.py hosted deploy hosted-main --tag v1.0.0
comfy_runner.py hosted deploy hosted-main --reset
```

## HTTP API Server Extensions

New routes on the local comfy-runner server (when managing hosted instances):

```
# Hosted instance lifecycle
POST   /hosted/init              → create hosted instance
POST   /hosted/<name>/start      → start/resume
POST   /hosted/<name>/stop       → stop (release GPU)
DELETE /hosted/<name>             → terminate
GET    /hosted/<name>/status      → status + URLs
GET    /hosted/list               → list all hosted instances

# Volume management
GET    /hosted/volumes            → list volumes
POST   /hosted/volumes            → create volume
DELETE /hosted/volumes/<name>     → delete volume
POST   /hosted/volumes/<name>/sync → sync models
```

## Implementation Structure

```
comfy_runner/
├── hosted/
│   ├── __init__.py
│   ├── provider.py              # HostedProvider protocol (abstract)
│   ├── runpod_api.py            # RunPod REST API client (pods, volumes, templates)
│   ├── runpod_s3.py             # RunPod S3 API client (model sync, cache)
│   ├── runpod_provider.py       # RunPod HostedProvider implementation
│   ├── config.py                # Hosted config schema + accessors
│   └── startup.py               # Pod startup script / entrypoint logic
```

## Implementation Phases

### Phase 1: Foundation
- `hosted/config.py` — hosted config schema, credential storage, volume registry
- `hosted/runpod_api.py` — REST API client (pod CRUD, volume CRUD)
- `hosted/provider.py` — HostedProvider protocol
- `hosted/runpod_provider.py` — RunPod provider implementation
- CLI: `hosted config`, `hosted volume create/list/rm`

### Phase 2: Instance Lifecycle
- `hosted/startup.py` — pod entrypoint (start comfy-runner server, detect fresh volume → run init)
- Docker image definition (Dockerfile for thin image)
- CLI: `hosted init`, `hosted start`, `hosted stop`, `hosted terminate`, `hosted status`, `hosted url`, `hosted list`
- Config: hosted installation records with pod_id, volume, URLs

### Phase 3: Model Sync
- `hosted/runpod_s3.py` — S3 client for volume file operations
- CLI: `hosted volume sync-models` (local↔volume, volume↔volume)

### Phase 4: Deploy + Full Integration
- Wire `hosted deploy` to call comfy-runner server API on the pod
- Server API routes for hosted operations
- Integration with existing `deploy`, `snapshot`, `nodes` commands via remote server

### Phase 5: Release Cache (Optimization)
- Cache volume management (create, maintain)
- Startup script checks cache volume before GitHub download
- Auto-eviction of old releases (keep last N)
- CLI: `hosted cache update` (pre-populate cache with latest releases)

### Future: Modal Backend
- Add `hosted/modal_provider.py` implementing the same HostedProvider protocol
- Modal best suited for headless/API-only mode (submit workflow JSON, get results)
- Uses Modal Volumes for model storage, Modal Tunnels for port exposure
- Different tradeoffs: true scale-to-zero, but fights interactive UI use case

## Key Constraints & Notes

- Network volumes are datacenter-locked (pod must be in same datacenter)
- One pod per volume at a time (no sharing for parallel instances)
- Volumes max 4 TB via API (contact support for larger)
- S3 API available in 4 datacenters: EUR-IS-1, EU-RO-1, EU-CZ-1, US-KS-2
- HTTP proxy has 100s Cloudflare timeout (WebSocket/async polling for long workflows)
- Pods with network volumes cannot be "stopped", only terminated — but the volume persists
- RunPod Secure Cloud required for network volumes
- User must create RunPod account and generate API keys manually (documented in setup guide)
