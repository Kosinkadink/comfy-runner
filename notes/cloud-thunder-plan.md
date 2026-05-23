# Thunder Compute provider — investigation & implementation plan

Status: **investigation only**, no code written yet. Save this somewhere
durable so we can pick it back up.

## Why we're considering Thunder Compute

Pain points with the current RunPod e2e test flow:

1. **Stop → resume is unreliable.** When a RunPod pod is `stopped`, the
   GPU on its host machine often gets reassigned. On `start` we wait
   indefinitely for the same host to free a GPU, or we scrap the pod and
   pay to provision a fresh one. Both options waste minutes per cycle.
2. **Re-downloading 1 TB of model weights** for every fresh pod is
   prohibitive. Network volumes help, but they're tied to a single
   datacenter, which makes the GPU-availability problem worse.
3. We want **fast cold-resume** for e2e tests that run < 10 hrs/day.

The two alternatives we evaluated were Lambda Cloud and Thunder Compute.

## Why not Lambda Cloud

- **No pause feature at all.** Docs explicitly: "It currently isn't
  possible to pause (suspend) your instance rather than terminating it.
  But, this feature is in the works." (Same line has been there for
  multiple years.)
- The persistent NFS filesystem ($0.20/GB/month, no quota) is fine for
  the 1 TB models requirement, but it must be attached at instance
  launch and is region-locked.
- H100/A100 availability is notoriously spotty in popular regions, so
  the "terminate + relaunch with FS attached" pattern can still strand
  you.

Net: Lambda is a sideways step, not forward.

## Why Thunder Compute looks promising

Per-minute billing, $0.78/hr A100-80GB, $1.38/hr H100-80GB (PCIe), no
egress, persistent disk on every instance, **snapshot-based resume that
restores onto whatever free host the scheduler picks** — so no
host-pinning at all. That eliminates the RunPod "stuck waiting for the
same host's GPU" problem entirely.

### Pricing recap (May 2026)

| GPU         | Prototyping | Production |
|-------------|-------------|------------|
| RTX A6000   | $0.27/hr    | $0.49/hr   |
| A100 80GB   | $0.78/hr    | ~$1.47/hr  |
| H100 80GB   | $1.38/hr    | ~$2.40/hr  |

- Persistent disk: **$0.15/GB/month** (100 GB included free)
- Snapshots: billed hourly by size
- Egress: **free**
- Disk cap: 400 GB (prototyping) / **1000 GB (production)** — 1 TB is a
  hard ceiling
- Per-minute billing (RunPod is per-second)
- For ~10 hrs/day of H100 production: ~$720/mo + $150/mo for 1 TB disk

### Hard trade-offs vs RunPod

- **North America only**, 1 datacenter. No EU/APAC.
- **A100 80GB / H100 80GB PCIe / RTX A6000 only.** No 4090s, no L40S,
  no SXM/NVLink H100s, max 8 GPU/instance.
- **Single-region status page**: 2 incidents in 90 days (April 28-29,
  network outage + slowdowns). Smaller operator, shorter track record.

## Architectural differences that drive the design

| Aspect              | RunPod                          | Thunder Compute                                  |
|---------------------|---------------------------------|--------------------------------------------------|
| Compute unit        | Docker container                | Ubuntu VM (SSH only)                             |
| Customization       | Pull our Docker image           | Bake setup into a snapshot of the persistent disk |
| Persistent storage  | Network volume at `/workspace`  | Per-instance persistent disk (≤1 TB) + `/ephemeral` |
| Pause/resume        | `stop`/`start` on **same host** | `snapshot` → `delete` → `create-from-snapshot` onto any free host |
| Restore cost        | seconds                         | **~8 min per 100 GB** ([snapshots docs](https://www.thundercompute.com/docs/cli/operations/snapshots)) |
| Inbound port        | RunPod proxy (we disable it) → Tailscale on pod | Native HTTPS subdomain per `add_ports` modify call |

The win: no host-pinning means resume reliability is high.
The trade-off: restore is multi-minute, so we batch restore once per
test session, not per workflow.

## Where do 1 TB of models live?

Three options:

- **A. Bake all models into the snapshot.** Simple, but every restore
  copies ~1 TB → ~80 min. Unworkable.
- **B. Slim "runner" snapshot (~30 GB) + `/ephemeral` model cache
  populated from S3/HF on first boot.** Fast restore (~3 min). Models
  re-download once per fresh instance. Persistent disk holds only OS +
  comfy-runner + ComfyUI checkout.
- **C. Per-suite snapshots with the models that suite needs.** Restore
  time scales with model set. Good for stable nightly suites.

**Strategy:** ship B first, layer C in as an optimization once the
provider works.

## Parallelism — the cost math (Option C)

For per-minute billing, with `R` = restore time per snapshot
(≈ 8 min per 100 GB), `T_i` = suite runtime, `N` = number of suites:

| Strategy | Wall clock | GPU-minutes billed |
|----------|------------|--------------------|
| Serial, one hot instance, no resnaps | R + ΣT_i | R + ΣT_i |
| Serial, one instance, restore between | N·R + ΣT_i | N·R + ΣT_i |
| **Parallel, N instances, per-suite snapshots** | R + max(T_i) | **N·R + ΣT_i** |

Parallel costs the **same as serial-with-restore-between**, finishing
in ~max(T_i) wall-clock instead of ΣT_i. Wins when:

- `R << T_i` (snapshots small)
- Suite runtimes imbalanced (serial is bottlenecked by the slowest
  anyway)
- Fast feedback matters (PR CI, nightly regression)

Wash or net loss when `R ≈ T_i` (e.g., 40 min restore for a 500 GB
snapshot to run a 15-min suite → ~3× cost).

**Implication:** keep per-suite snapshots small (carry only the models
that suite uses, typically 20–80 GB). Don't bake the whole 1 TB library
into every snapshot.

## What the API actually exposes

Full Thunder Compute API surface is just **14 endpoints**:

```
POST   /instances/create
GET    /instances/list
POST   /instances/{id}/add_key
POST   /instances/{id}/delete
POST   /instances/{id}/modify
POST   /snapshots/create
GET    /snapshots/list
DELETE /snapshots/{id}
POST   /keys/add
GET    /keys/list
DELETE /keys/{id}
GET    /pricing
GET    /specs
GET    /thunder-templates
```

Source: <https://github.com/Thunder-Compute/thunder-compute-documentation/blob/main/swagger.json>

### Confirmed from swagger

- **No `start`/`stop` endpoints exist.** Lifecycle is `create` + `delete`
  + `modify`. "Pause" = `snapshots/create` then `instances/{id}/delete`.
  "Resume" = `instances/create` with `template = <snapshot_id>`.
- `template` field on `InstanceCreateRequest` accepts both base
  templates and user snapshot IDs — snapshot-as-template is unified.
- `InstanceListItem` reports `provisioningTime`, `restoringTime`,
  `snapshotSize`, `storage`, `httpPorts`, `port`, `ip`, and `status`.
  We can measure restore latency empirically.
- `InstanceCreateResponse` returns a generated SSH `key` (private key),
  or we can pass `public_key` on create. Either flow works.
- `InstanceModifyRequest` supports `add_ports` / `remove_ports` and
  changing `gpu_type` / `num_gpus` / `cpu_cores` / `disk_size_gb` on a
  live instance — hot-swap is real.
- `CreateSnapshotRequest` only needs `instanceId` + `name`; whole-disk
  snapshot, no selective option.
- `GpuLimits` schema is per-GPU CPU/RAM caps, **not** account quotas.

### Native HTTPS port forwarding — Tailscale not needed

`InstanceListItem.httpPorts` (list of ints) is exposed. Calling
`POST /instances/{id}/modify` with `add_ports=[8188,9189]` opens those
ports as `https://<uuid>-<port>.thundercompute.net` automatically
(per their port-forwarding docs).

This is **simpler than the RunPod setup**, where we had to run
Tailscale-in-userspace on the pod because RunPod's proxy was
unreliable. Thunder gives us managed HTTPS out of the box.

### Reliability data

- [status.thundercompute.com](https://status.thundercompute.com): 99.95%
  reported instance uptime
- [status history](https://status.thundercompute.com/history): only 2
  incidents in 90 days (network slowdowns/outage on April 28-29 2026).
  Feb / March / May had zero incidents.
- **Caveat:** small operator (YC 2024 batch), short track record,
  single-region. Any DC issue is total.

### Unanswered questions (need empirical or support answer)

1. **Per-account concurrent-instance cap** — not in API, not in
   [restrictions docs](https://www.thundercompute.com/docs/restrictions).
2. **Concurrent restore queueing** — whether 4 parallel
   `create-from-snapshot` calls actually restore in parallel or
   serialize on their backend.
3. **Real GPU availability for 4× H100 simultaneously** in NA region.
4. **Whether `RESTORING` state is billed.** Billing docs say "per
   minute while instances run." Restoring is technically a running
   instance, so almost certainly yes — worst case for the cost model.

## Implementation plan

Mirrors the existing RunPod layout in
[comfy_runner/hosted/](../comfy_runner/hosted) and
[comfy_runner/testing/](../comfy_runner/testing).

```
comfy_runner/hosted/
├── provider.py              (existing protocol — already abstract)
├── runpod_provider.py       (existing)
├── thunder_api.py           NEW: thin REST client over swagger.json
├── thunder_provider.py      NEW: implements HostedProvider protocol
└── config.py                EXTEND: get_thunder_api_key, defaults

comfy_runner/testing/
├── runpod.py                (existing)
└── thunder.py               NEW: orchestrates snapshot/restore/test
```

### Step 1 — `thunder_api.py` (~150 LOC)

Bearer-token REST wrapper for the 14 endpoints. Generate stubs directly
from the published swagger.

### Step 2 — `thunder_provider.py` (~250 LOC)

Implement `HostedProvider` protocol from `provider.py`. Mapping:

| Protocol method   | Thunder call                                             |
|-------------------|----------------------------------------------------------|
| `create_pod`      | `POST /instances/create` with template = base snapshot   |
| `start_pod`       | `POST /instances/create` from per-pod snapshot (resume)  |
| `stop_pod`        | `POST /snapshots/create` then `POST /instances/{id}/delete` |
| `terminate_pod`   | Optional snapshot, then `POST /instances/{id}/delete`    |
| `get_pod_url`     | Compose `https://<uuid>-<port>.thundercompute.net`        |
| `create_volume`   | `POST /snapshots/create` (snapshots **are** our volumes)  |

### Step 3 — first-boot bootstrap

`comfy_runner/hosted/thunder_bootstrap.sh`, analogous to `startup.sh`:

- `pip install comfy-runner`, clone ComfyUI, mount `/ephemeral` for
  model cache, start comfy-runner server on :9189.
- One-time manual step: `tnr create`, SSH in, run bootstrap, then
  `tnr snapshot create --name comfy-runner-base`. Document in README.
- Persist snapshot ID in `~/.comfy-runner/hosted.json` under
  `thunder.base_snapshot_id`.

### Step 4 — `testing/thunder.py` (~300 LOC)

Mostly a copy of `runpod.py`'s `run_on_runpod` with adjustments:

- **Pod naming:** `tnr` uses numeric IDs, not stable names — we maintain
  our own `name → instance_id` map in `pod_record` via
  `set_pod_record("thunder", …)`.
- **Reuse logic:** if a snapshot exists for the pod name, restore from
  it (~3 min); otherwise fresh create from base snapshot.
- **Watchdog teardown:** on overrun, `snapshot create` then `delete`.
- **`_wait_for_server`:** bump restore timeout to ~600 s (vs RunPod's
  300 s) since cold-restore takes longer.

### Step 5 — CLI plumbing

- Add `--target thunder:<gpu_type>` to `test` and `test fleet` commands
  (`thunder:a100-80gb`, `thunder:h100`, `thunder:a6000`).
- Add `hosted config set thunder.api_key …` and
  `thunder.base_snapshot_id …` via existing helpers.

### Step 6 — Tests / docs

- Unit-test `thunder_api.py` with `responses` mocks (same pattern as
  `runpod_api.py`).
- Document the one-time snapshot setup in [README.md](../README.md)
  Hosted section.
- Smoke test: `comfy-runner test run hello-world --target thunder:a100-80gb`.

### Step 7 — Per-suite snapshot registry (Option C, parallel)

Schema in `~/.comfy-runner/hosted.json`:

```json
{
  "thunder": {
    "base_snapshot_id": "snap-abc",
    "suite_snapshots": {
      "video-generation": "snap-def",
      "image-upscale":    "snap-ghi"
    }
  }
}
```

New CLI: `comfy-runner hosted thunder snapshot build <suite>` —
provisions instance, runs `preflight.ensure_suite_models`, snapshots,
stores ID.

`test fleet <suite> --target thunder:h100 --target thunder:h100`
already works once the Thunder provider exists; each parallel job picks
the matching `suite_snapshots[suite]` automatically.

## Reliability risks & mitigations

1. **Restore-time GPU contention.** Single region → if A100s are out at
   2am, restore queues. **Mitigation:** `gpu_type` as a fallback list
   (`["a100-80gb", "a6000"]`), pick first available.
2. **8 min/100 GB restore.** **Mitigation:** keep base snapshot <50 GB;
   pull models to `/ephemeral` on first ComfyUI run, not in snapshot.
3. **`RESTORING` likely billed.** Parallelism multiplies this cost.
   **Mitigation:** small per-suite snapshots only when parallelism
   actually helps (`R << T_i`).
4. **Tailscale unused on Thunder** — confirm Cloudflare-style HTTPS
   subdomain auth meets our threat model (probably yes; it's their
   intended pattern).

## Effort estimate

- ~2 days: API client + provider + bootstrap script
- ~1 day: `testing/thunder.py` + CLI wiring
- ~1 day: snapshot creation, port-forwarding smoke tests, e2e
- ~0.5 day: docs

**Total: ~4-5 days** to feature parity with RunPod e2e tests, modulo
the restore-time floor (~3 min cold-start vs RunPod's ~45 s).

## Recommended next step before committing

Write a small standalone Python script (~150 LOC) that hits the Thunder
REST API directly to validate:

1. Create one instance from base Ubuntu, install our stuff, snapshot →
   record snapshot size + creation duration.
2. From that snapshot, fire 4 `instances/create` calls simultaneously
   → record `restoringTime` for each, confirm GPU allocations, observe
   whether they queue.
3. Tear all down, sum the bill.

That gives us hard numbers for `R` (restore time) and confirms parallel
feasibility before investing 4-5 days on full integration.

In parallel: email support@thundercompute.com to ask about per-account
concurrent-instance limits and concurrent-restore policy.
