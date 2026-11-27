# Selfheal-LLM: a self-healing security pipeline for LLMs

**selfheal-llm** attacks a LLM (`qwen2.5:3b` via Ollama) with
[garak](https://github.com/NVIDIA/garak) probes, patches its defences automatically and
measures the result on held-out data. 

**Defence layers**

1. **Input filter**: regex rule sets (prompt injection, jailbreak personas, encoded payloads)
   plus a prompt-injection classifier (`protectai/deberta-v3-base-prompt-injection-v2`) with
   a tunable threshold and length gate.
2. **System prompt**: a hardened prompt with fixed security rules plus a "learned rules"
   section that an optimizer LLM may edit.
3. **Output filter**: a persona/policy keyword filter plus a toxicity classifier
   (`unitary/toxic-bert`) with a tunable threshold.

**The self-healing loop**

The loop searches over these layers: toggles, threshold steps, small combinations, and
optimizer-written prompt rules. A two-objective rule (attack success rate vs. benign false
positives) decides on a validation split. The final numbers come from a test split and from
garak probe families the loop never saw.

**Headline results**

| | Attack success rate (ASR) | Benign false-positive rate (FPR) |
|---|---|---|
| Baseline (no defences) | 39.6 | 0.0 |
| Self-healed | 0.0 | 4.0 |

---

## Requirements

| Item | Tested setup |
|---|---|
| OS | Windows 11, PowerShell (main path). Linux works with Docker (Option B). |
| Python | 3.11 or 3.12 (64-bit) |
| GPU | NVIDIA GPU with ≥ 6 GB VRAM and a current NVIDIA driver |
| Disk | about 10 GB free |
| Network | needed once, for the model, Python packages and classifier downloads |

---

## Option A: local setup without Docker (main path)

### A1. Install Ollama and pull the model

1. Install Ollama from <https://ollama.com/download> and start the Ollama app.
2. Pull the model and test it:

```powershell
ollama pull qwen2.5:3b
ollama run qwen2.5:3b "Say hello in one sentence."
ollama ps                     
```

### A2. Create the virtual environment and install pinned dependencies

```powershell
git clone git@github.com:naienim/Selfheal-LLM-Security.git
cd selfheal-llm
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
```
Choose **one** of the options below based on your hardware.

#### Option 1: CPU Only

```bash
python -m pip install -r requirements-torch.txt --index-url https://download.pytorch.org/whl/cpu
```

#### Option 2: GPU (NVIDIA CUDA 12.4)

Requires an NVIDIA GPU with drivers supporting CUDA 12.4.

```bash
python -m pip install -r requirements-torch.txt --index-url https://download.pytorch.org/whl/cu124
```

To install the GPU version directly without using the requirements file:

```bash
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
```

### A3. Verify the setup

```powershell
python --version                                        # 3.11.x or 3.12.x
python -m garak --version                               # garak 0.17.1
python -c "import torch, transformers, yaml, pandas, matplotlib; print('imports ok, torch', torch.__version__)"
python -m pip check                                     # "No broken requirements found."
nvidia-smi                                              # your GPU is listed
ollama list                                             # qwen2.5:3b is listed
```

It worked if every command succeeds and `ollama ps` showed 100% GPU in step A1.

---

## Option B: Docker setup (alternative path)


**Prerequisites**
- **Windows:** Docker Desktop with the WSL 2 backend and an NVIDIA driver with WSL support.
- **Linux:** Docker Engine with the Compose plugin, plus the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
- **Check GPU access:** the following command must print your GPU.

  ```bash
  docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
  ```

- **Close a locally running Ollama app first.** Two Ollama servers on one 6 GB GPU will run
  out of memory.

**What runs**

| Service | Role |
|---|---|
| `ollama` | Ollama server; models persist in the `ollama-models` volume |
| `ollama-pull` | Pulls `qwen2.5:3b` once, then exits |
| `pipeline` | Python, garak and the guards |

The `pipeline` service shares the network of `ollama`, so the default
`base_url: http://127.0.0.1:11434` in `configs/base.yaml` works unchanged.

These are stored on your host and persist after the containers stop:
- bind mounts: `configs/`, `data/`, `results/`, `logs/` and `README.md`;
- named volumes: Hugging Face and garak caches (`hf-cache`).

```bash
docker compose build
docker compose up -d ollama                # start Ollama (GPU)
docker compose run --rm ollama-pull        # pull qwen2.5:3b (about 2 GB)
docker compose run --rm pipeline python -m scripts.smoke_test
```

To use the API from the host for debugging, Ollama is also published on `127.0.0.1:11435`.
To stop everything, run `docker compose down`. Models and caches stay in their volumes; add
`-v` to delete them.

---

## Usage

Every step below runs as `python -m scripts.<name>` in Option A.

In Option B, prefix the same command with `docker compose run --rm pipeline`, for example
`docker compose run --rm pipeline python -m scripts.heal_search`.

Each command writes a log file to `logs/<timestamp>_<step>.log`.

### 1. Smoke test

```powershell
python -m scripts.smoke_test
python -m scripts.spike_garak_detectors
```

- **What it does:**
  - The smoke test checks Ollama, both guards and one tiny garak scan per category, then
    prints a runtime estimate.
  - The spike checks that garak detectors score correctly inside Python.
- **Output:** `results/experiments/smoke/`. Success shows 7 `[PASS]` lines, then 8 `[PASS]`
  lines.

### 2. Fixed data splits

```powershell
python -m scripts.build_splits
```

- **What it does:** records attack prompts once (no model calls), groups near-duplicates,
  and splits attacks and benign prompts into improvement / validation / test sets. The
  splits stay fixed for every later step.
- **Output:** `data/splits/` (`manifest.json` + JSONL files). Use `--force` only if you
  deliberately want new data.

### 3. Ablation experiments (held-out evaluation)

```powershell
python -m scripts.run_experiment --config configs/experiments/baseline.yaml
python -m scripts.run_experiment --config configs/experiments/prompt_only.yaml
python -m scripts.run_experiment --config configs/experiments/output_only.yaml
python -m scripts.run_experiment --config configs/experiments/both.yaml
```

- **What it does:** evaluates one fixed configuration on exactly the same held-out data:
  - the test split (attacks + 25 benign prompts), scored with garak's detectors;
- **Output:**
  - `results/final_test/configs/<name>.json` and `<name>_test_items.json`;
  - merged into `results/final_test/summary.json` and `final_test.md`.

### 4. Self-healing loop

```powershell
python -m scripts.heal_search
```

- **What it does:**
  - Starts with no defences.
  - Cheap move search: toggles, threshold steps and combinations, each kept or rolled back
    by the acceptance rule on the validation split.
  - Then the prompt optimizer: learned rules, judged with and without the guards.
  - Restores the best checkpoint at the end.
- **Output:**
  - `results/search/run_<timestamp>/` (`candidates.jsonl`, threshold curves, optimizer
    iterations, checkpoints);
  - `results/search/summary.json`;
  - the healed configuration `configs/experiments/healed_search.yaml`.

### 5. Final test of the healed configuration

```powershell
python -m scripts.run_experiment --config configs/experiments/healed_search.yaml
```

- **What it does:** evaluates the healed configuration on the same held-out data as the
  ablations. (`python -m scripts.final_test` re-tests all five configurations in one batch.)
- **Output:** `results/final_test/` (merged with the ablations).

The test log refuses to re-test a configuration that changed after it was tested.
`--force` overrides this.

### 6. Analysis: tables, plots and report

```powershell
python -m scripts.analyze
```

- **What it does:** builds every table, plot, qualitative example and a written report from
  saved results, with no model calls.
- **Output:**

  | Location | Contents |
  |---|---|
  | `results/analysis/report.md` | Main document |
  | `results/analysis/plots/*.png` |  analysis plots |
  | `results/analysis/tables/*.csv` | Every table |
  | `results/analysis/examples_test_split.md` | Qualitative examples |

### 7. Everything with one command

```powershell
python -m scripts.run_pipeline                    # Option A
docker compose up pipeline                        # Option B
```

- **What it does:** runs spike → splits → 4 ablations → healing loop → healed final test →
  analysis. It stops at the first failing step and logs each step's start, end and duration.
- **Options:**
  - `--skip-existing` resumes after an interruption (cached answers are reused);
  - `--skip-heldout` is faster (test split only);
  - `--max-iterations N` limits the search.
- **Output:** all of the above, plus `logs/<timestamp>_run_pipeline.log`.

---

## Configuration

Experiment files use `configs/base.yaml` and override only what differs.

### `configs/base.yaml`

| Setting | Meaning |
|---|---|
| `target.model`, `temperature`, `num_predict`, `num_ctx` | Model and sampling. Changing them invalidates the answer cache. |
| `system_prompts.baseline / hardened / learned_rules` | System prompts. `learned_rules` is the part the optimizer edits. |
| `mitigations.*` | Default (all off): regex rule sets, classifier `threshold` / `min_words`, output `policy` / `toxicity` (`threshold`), fallback messages |
| `garak.categories` | Probe families per category (prompt injection, jailbreak, toxicity) |
| `splits.seed`, `*_fractions`, `max_prompts_per_probe` | How the fixed splits are built (seed and sizes) |
| `final_test.heldout_categories` | Probe families used **only** for the held-out test |
| `evaluator.scoring_mode` | `inprocess` (fast) or `subprocess` (fallback) |

### `configs/healing_search.yaml`

| Setting | Meaning |
|---|---|
| `search.objective`, `fpr_weight` | What "better" means (ASR + λ·FPR); lower λ favours blocking attacks over avoiding false positives |
| `search.max_fpr` | Hard limit: no kept configuration may exceed this validation FPR |
| `search.min_delta_attacks`, `min_delta_attacks_prompt`, `min_delta_benign` | How large a change must be to count (prompt changes need more because of sampling noise) |
| `search.max_iterations`, `max_move_iterations`, `patience` | Search budget and stopping |
| `search.toggles` | Defences the search may switch on or off; remove one to keep it fixed |
| `search.thresholds` | Numeric settings with step size, step halving and range |
| `search.max_combo_size`, `combo_top_k`, `max_candidates_per_iteration` | How many changes are combined and tried per iteration |
| `optimizer.model`, `learn_from`, `accept_on_model_level`, `candidates` | Prompt optimizer: its model, what it learns from, and when its edits are kept |

### `configs/experiments/*.yaml`

The four ablations are fixed and hand-configured. `healed_search.yaml` is generated; don't
edit it by hand, because the next search overwrites it.