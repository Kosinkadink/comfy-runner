# `comfyrunner` Manifest Spec

A *manifest* tells `comfy-runner review` everything it needs to provision a ComfyUI installation for reviewing a PR: which workflow files to drop into `user/default/workflows/`, and which models to download into `models/<directory>/<name>`.

The canonical place to publish a manifest for a PR is a fenced **`comfyrunner`** code block inside the PR description on GitHub. The same JSON shape is also accepted standalone (a `.json` file fed to `review-validate`, or programmatically via the HTTP API).

## Quick example

````markdown
```comfyrunner
{
  "workflows": [
    "https://raw.githubusercontent.com/comfyanonymous/ComfyUI/<branch>/examples/sdxl_demo.json"
  ],
  "models": [
    {
      "name": "sdxl_base.safetensors",
      "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
      "directory": "checkpoints"
    }
  ]
}
```
````

`comfy-runner` accepts both `comfyrunner` and `comfy-runner` as the fence language tag.

## Schema

```jsonc
{
  "workflows": [           // optional; default []
    "<https URL>"          // strings, one per workflow file to fetch
  ],
  "models": [              // optional; default []
    {
      "name":      "<filename>",       // required; bare filename, no slashes, not '.' / '..'
      "url":       "<https URL>",      // required; HTTPS only
      "directory": "<relative path>"   // required; under ComfyUI's models/, e.g. "checkpoints", "loras"
    }
  ]
}
```

The top level must be a JSON object. Both `workflows` and `models` are optional, but a manifest with neither is meaningless and `review-validate` will warn.

### `workflows[]`

Each entry is a fully-qualified HTTPS URL to a ComfyUI workflow JSON file. `review` downloads each URL into the target installation's `user/default/workflows/` directory; the filename is derived from the URL's last path segment (run through the project's path-traversal sanitizer).

A successfully fetched workflow is also scanned for embedded `node.properties.models` entries; those entries get merged into the model list automatically (deduped by `(directory, name)`), so a typical workflow that already declares its models in the node properties needs **zero** entries in `models[]`.

### `models[]`

Each entry has three required fields:

| Field       | Constraints                                                                 |
|-------------|-----------------------------------------------------------------------------|
| `name`      | A safe filename — no path separators, not `.` or `..`. Saved verbatim.      |
| `url`       | Must be HTTPS. Hosts in the default allowlist (see below) are always OK.    |
| `directory` | Relative path under `ComfyUI/models/`. No traversal, no absolute paths.     |

Models are downloaded to `<models_dir>/<directory>/<name>`. Existing files at that location are skipped. Authentication is automatic for `huggingface.co` and `modelscope.cn` (via the `HF_TOKEN` / `MODELSCOPE_SDK_TOKEN` env vars); other hosts can take a token via the CLI's `--token` flag.

## URL allowlist

By default `comfy-runner` only fetches workflows and models from this host list:

- `huggingface.co`
- `civitai.com`
- `modelscope.cn`
- `gist.githubusercontent.com`
- `raw.githubusercontent.com`
- `github.com`

Subdomains match too (`cdn-lfs.huggingface.co` matches `huggingface.co`). Anything else triggers a warning during `review-validate` and a hard refusal during `review` unless the operator passes `--allow-arbitrary-urls`. If you need a host that's missing, file a request rather than relying on the override.

## Priority and merging

When `review` runs, it builds the final fetch list by merging three sources:

1. **CLI flags** (`--workflow`, `--model`) — highest priority. Useful for one-off overrides without editing the PR description.
2. **PR-body `comfyrunner` block** — the canonical source maintained by the PR author.
3. **Embedded `node.properties.models`** in fetched workflows — auto-discovered.

Deduplication keys: workflows by URL, models by `(directory, name)`. Earlier sources win on conflict.

## Authoring workflow

The recommended cycle for PR authors:

1. Open ComfyUI, build the workflow you want to demo, save it as a JSON file in your branch (e.g. `examples/sdxl_demo.json`).
2. Run `comfy_runner.py review-init examples/sdxl_demo.json --workflow-url https://raw.githubusercontent.com/<owner>/<repo>/<branch>/examples/sdxl_demo.json`. The command pulls models out of `node.properties.models` for you.
3. Paste the printed block into your PR description.
4. Run `comfy_runner.py review-validate <owner>/<repo>#<pr>` to confirm the block is well-formed and all URLs are reachable / on the allowlist.
5. Reviewers run `comfy_runner.py review <pr> --repo <owner>/<repo> --target ...` to provision an installation against your PR.

## Common mistakes (and what `review-validate` says about them)

| Mistake                                                | Severity | Message                                                  |
|--------------------------------------------------------|----------|----------------------------------------------------------|
| `http://...` instead of `https://...`                  | error    | `model URL is not HTTPS`                                 |
| Model name like `subdir/model.safetensors`             | error    | `model name must be a bare filename`                     |
| `directory: "/etc/models"` (absolute path)             | error    | `model directory must be a relative path`                |
| `directory: "../etc"` (path traversal)                 | error    | `model directory must be a relative path`                |
| Missing required field on a model entry                | error    | `model entry missing 'url'` (etc.)                       |
| URL host like `example.com` (not in allowlist)         | warn     | `URL host is not in the default allowlist`               |
| Empty manifest (no workflows, no models)               | warn     | `manifest is empty`                                      |
| PR body contains no `comfyrunner` block                | info     | `no comfyrunner block found in this source`              |

## See also

- [`README.md`](../README.md) — full CLI reference and the `review` command.
- [`comfy_runner/manifest.py`](../comfy_runner/manifest.py) — schema validation, GitHub PR fetch, workflow URL fetch.
- [`comfy_runner/review_authoring.py`](../comfy_runner/review_authoring.py) — `review-init` and `review-validate` implementations.
