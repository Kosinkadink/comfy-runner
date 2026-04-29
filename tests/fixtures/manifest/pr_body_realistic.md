# Add awesome new node

This PR adds a new node that does foo and bar. Reviewers can try it
with the workflow below.

## How to test

1. Run the linked workflow.
2. Compare with the screenshot below.

```comfyrunner
{
  "models": [
    {
      "name": "explicit.safetensors",
      "url": "https://huggingface.co/test/explicit.safetensors",
      "directory": "checkpoints"
    }
  ],
  "workflows": [
    "https://gist.githubusercontent.com/u/abc/raw/wf-realistic.json"
  ]
}
```

## Screenshot

![demo](https://example.com/demo.png)

```python
# Some unrelated code block, must NOT match the manifest regex.
print("hello")
```
