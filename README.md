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
python status.py --coordinator http://localhost:5000

# Live-Ansicht (alle 10 s)
python status.py --coordinator http://localhost:5000 --watch
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
# Einmalige Anzeige
python status.py --coordinator http://10.0.0.1:5000

# Live-Ansicht (alle 10 s)
python status.py --coordinator http://10.0.0.1:5000 --watch
```

Beispielausgabe:
```
────────────────────────────────────────────────
  Caption Queue  —  http://10.0.0.1:5000
────────────────────────────────────────────────
  [████████░░░░░░░░░░░░░░░░░░░░░░]  20.8%
  Done:           83 / 400
  In progress:     5
  Queued:        312

  Worker                Status    Current shard                   Last seen
  mac-studio-1          ✓ online  shard_042.parquet               4s ago
  mac-studio-2          ✓ online  shard_107.parquet               7s ago
  mac-studio-3          ✗ offline —                               23m ago
────────────────────────────────────────────────
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

## Bekannte Probleme

### AttributeError: all_tied_weights_keys (transformers zu neu)

Molmo-7B-D-0924 ist nicht kompatibel mit transformers ≥ 4.46. Fix:

```bash
~/miniforge3/envs/caption-worker/bin/pip install "transformers>=4.40.0,<4.46.0"
```

Das install_worker.sh pinnt die Version automatisch — bei manueller Installation darauf achten.

---

### tensorflow fehlt beim ersten Install

Molmo benötigt `tensorflow` zur Initialisierung des Processors. Falls der Install abbricht mit:

```
ImportError: This modeling file requires the following packages that were not found: tensorflow
```

Manuell nachinstallieren und Install-Script nochmal ausführen:

```bash
~/miniforge3/envs/caption-worker/bin/pip install tensorflow
bash install_worker.sh --coordinator http://... --worker-name ...
```

**Hinweis:** Auf Apple Silicon lädt tensorflow ~32 GB — das ist einmalig und normal.

---

## Requirements

**Coordinator:** Python 3.10+, Flask, requests  
**Workers:** Python 3.11, PyTorch (MPS/CUDA/CPU), Transformers ≥ 4.40, tensorflow, ~50 GB disk (14 GB Modell + 32 GB tensorflow)
