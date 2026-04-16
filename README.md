# distributed-caption

Lightweight distributed image captioning with **Molmo 7B**.

A coordinator server distributes parquet shards to worker machines on demand.
Workers caption every image, return JSONL results, and keep minimal disk usage —
only one shard at a time is stored locally (~300 MB + 14 GB model weights).

Works on **CUDA** (Linux/Windows), **MPS** (Apple Silicon), and **CPU**.

---

## Architecture

```
Coordinator (Linux server / 4090)
  ├── holds all parquet files
  ├── serves one shard at a time per worker
  └── collects JSONL results

Workers (Mac Studios, any number)
  ├── download shard → /tmp  (~300 MB)
  ├── caption with Molmo 7B
  ├── upload JSONL → coordinator
  └── delete local shard
```

Workers register automatically and pick up the next available shard as soon as
they finish the current one. If a worker goes offline, its shard returns to the
queue after 30 minutes.

---

## Quick start

### 1 — Coordinator (run once, on your server)

```bash
# Install dependencies
pip install flask requests tqdm pyarrow pillow

# Start coordinator
python coordinator.py \
    --parquet-dir /data/raw/pd12m \
    --output-dir  /data/captions/pd12m \
    --port 5000
```

Check progress at any time:
```bash
curl http://localhost:5000/status | python -m json.tool
```

### 2 — Workers (run once per Mac Studio)

On each Mac, run the installer — it sets up the conda environment, downloads
the Molmo model, and installs a **LaunchAgent** that starts the worker
automatically on every login.

```bash
# From the Mac itself (requires this repo to be present):
bash install_worker.sh \
    --coordinator http://10.0.0.1:5000 \
    --worker-name mac-studio-1

# Or push from the coordinator via SSH (one-liner):
ssh administrator@10.0.0.x \
    "bash <(curl -fsSL http://10.0.0.1:5000/install_worker.sh) \
     --coordinator http://10.0.0.1:5000 \
     --worker-name mac-studio-1"
```

The worker starts immediately and will restart automatically after crashes or reboots.

#### Worker commands

```bash
# Watch live log
tail -f ~/caption-worker/logs/worker.log

# Stop worker
launchctl unload ~/Library/LaunchAgents/com.pd.caption-worker.plist

# Start worker
launchctl load   ~/Library/LaunchAgents/com.pd.caption-worker.plist
```

---

## Monitoring

```bash
# All workers + queue status
curl http://10.0.0.1:5000/status | python -m json.tool

# Example output:
# {
#   "shards":  {"queued": 312, "in_progress": 5, "done": 83},
#   "total":   400,
#   "workers": {
#     "mac-studio-1": {"last_seen": 1713300000, "current_shard": "part0/shard_042.parquet"},
#     "mac-studio-2": {"last_seen": 1713300010, "current_shard": "part0/shard_107.parquet"}
#   }
# }
```

---

## Output format

One JSONL file per parquet shard, saved to `--output-dir`:

```jsonl
{"key": "abc123", "caption": "A photograph of a mountain lake at sunset…"}
{"key": "def456", "caption": "A close-up of red roses in a garden…"}
```

To merge all results into a single file:
```bash
cat /data/captions/pd12m/*.jsonl > captions_all.jsonl
```

---

## Disk usage per worker

| Item | Size |
|---|---|
| Molmo 7B weights (one-time) | ~14 GB |
| One parquet shard (in /tmp) | ~200–500 MB |
| JSONL result (before upload) | ~5 MB |
| **Total permanent** | **~14 GB** |

---

## Adding a new dataset

Just point the coordinator at a different parquet directory — the workers need no changes:

```bash
python coordinator.py \
    --parquet-dir /data/raw/custom_dataset \
    --output-dir  /data/captions/custom_dataset
```

---

## Requirements

**Coordinator:** Python 3.10+, Flask, requests  
**Workers:** Python 3.11, PyTorch (MPS/CUDA/CPU), Transformers ≥ 4.40, ~14 GB disk
