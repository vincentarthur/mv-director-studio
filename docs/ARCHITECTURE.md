# 📘 Gemini 3.8 Flash & 4 Argon — MV 评测、Storyline 创作与 MV 全链路生成平台 (V2.3) 完整架构与配置手册

> **⚡ 快速执行提示**：如果您希望直接查看按场景分类的 **3 步快速启动与部署命令**，请优先打开同目录下的：
> 👉 **[`docs/QUICKSTART.md`](./QUICKSTART.md)**

---

## 📑 目录导航 (Table of Contents)

1. **[一、快速选型建议与命令速查表 (Quick Cheat Sheet)](#一快速选型建议与命令速查表-quick-cheat-sheet)**
2. **[二、平台架构与 8-Tab 核心功能说明（含 V2.3 新增 MV 全链路生成工坊）](#二平台架构与-8-tab-核心功能说明含-v23-新增-mv-全链路生成工坊)**
3. **[三、部署前置条件 (Prerequisites)：需要什么权限？](#三部署前置条件-prerequisites需要什么权限)**
   - 3.1 当前执行部署的用户 (Deployer) 所需权限
   - 3.2 Cloud Run 运行时服务账号 (`argon-mv-eval-sa`) 所需权限
4. **[四、多源素材与存储机制：样本不在（或在）GCS 上如何访问？](#四多源素材与存储机制样本不在或在-gcs-上如何访问)**
   - 4.1 场景 A：样本【不在】GCS 上（无 GCS 模式）
   - 4.2 场景 B：样本【在】GCS 上（GCS 路径规范、`REGION` 要求与建桶命令）
5. **[五、Cloud Run 部署与三种安全访问方案详解](#五cloud-run-部署与三种安全访问方案详解)**
   - 方案一：`gcloud run services proxy`（适合自己与内部团队，IAM 私密模式）
   - 方案二：内置零明文 `SHA-256` 口令门禁（适合发链接给外部客户免装 `gcloud`，**无需配置 OAuth Consent**）
   - 方案三：Cloud Run 原生 `IAP`（适合企业客户用 Google 账号白名单登录，附 OAuth Consent 说明）
6. **[六、V2.1 / V2.2 评分规则白皮书（可直接发给客户/业务方）](#六v21--v22-评分规则白皮书可直接发给客户业务方)**

---

## 一、快速选型建议与命令速查表 (Quick Cheat Sheet)

> **💡 重点澄清（关于方案二 vs 方案三是否需要配置 OAuth Consent Screen）**：
> - **如果您希望外部客户点开链接就能直接体验，且不想配置 OAuth Consent Screen**：强烈推荐使用 **【方案二：内置零明文 SHA-256 口令门禁】**。该方案由应用自身渲染加密口令验证页，**完全不经过 Google OAuth，因此 100% 不需要配置 OAuth Consent Screen**，客户也不需要拥有 Google 账号或安装 `gcloud`（仅要求您的 GCP 项目未通过组织策略禁止 `--allow-unauthenticated`）。
> - **如果您的企业 GCP 组织策略强制禁止 `--allow-unauthenticated`**：
>   - 仅内部人员访问 👉 用 **【方案一：`gcloud run services proxy`】**（无需 OAuth Consent Screen）；
>   - 需让用户直接用浏览器登录 Google 账号访问 👉 用 **【方案三：Cloud Run 原生 IAP】**（同组织内部员工访问无需手动配 OAuth Screen；仅当允许组织外部客户邮箱登录时，需在 GCP Console 将 OAuth Consent Screen 设为 `External`）。

| 您的目标场景 | 直接复制执行的命令 | 访问方式与特点 |
| :--- | :--- | :--- |
| **1. 本地电脑 / Cloudtop 直接运行** | `pip install -r requirements.txt`<br>`export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"`<br>`python3 main.py --port 8095` | 浏览器打开 `http://127.0.0.1:8095`（30 秒启动） |
| **2. 部署到 Cloud Run 给【外部客户】看**<br>*(方案二：零明文 SHA-256 口令门禁，免 OAuth)* | `export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"`<br>`PROMPT_PASSCODE="true" ./deploy.sh`<br>*(按提示隐藏输入访问口令)* | **无需配置 OAuth Consent、无需装 `gcloud`**。客户点开输出的 `https://xxx.run.app` 输入口令即可进入 |
| **3. 部署到 Cloud Run 给【自己/队友】看**<br>*(方案一：公网 403 私密模式)* | `export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"`<br>`./deploy.sh` | 本地运行 `gcloud run services proxy argon-mv-eval-v2 --project=genai-dnb-demo --region=us-central1 --port=8095`，打开 `http://127.0.0.1:8095` |
| **4. 部署到 Cloud Run + 企业级 IAP**<br>*(方案三：Google 账号白名单)* | `export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"`<br>`ENABLE_IAP="true" ./deploy.sh` | 授权白名单邮箱后，用户直接打开 `https://xxx.run.app` 通过 Google 账号登录访问 |

---

## 二、平台架构与 8-Tab 核心功能说明（含 V2.3 新增 MV 全链路生成工坊）

本平台用于系统性评测 **Gemini 3.8 Flash (`gemini-3.8-flash`)** 与 **Gemini 4 Argon (`gemini-4-argon`)** 在音乐 MV 场景下的 **多模态音频理解、分镜脚本策划、提示词自动优化、基于词曲情绪的 MV Storyline 故事线生成与对比打分，以及 V2.3 全新加入的 MV 全链路自动生成（分镜规划 → 毫秒级口型节奏控制 → 分镜渲染 → 双语字幕同步与 FFmpeg 合并）**。

### 2.1 八大功能模块 (8-Tab 工作台)

| Tab 编号 | 模块名称 | 核心功能说明 |
| :--- | :--- | :--- |
| **Tab #1** | **Step 1: 原Prompt + 3.8 Flash** | 使用未修改的原版《音频理解-提示词.txt》，对 01–10 测试集并行调用 `gemini-3.8-flash`，按《MV评判规则.md》三层 13 维度自动打分。 |
| **Tab #2** | **Step 2: 原Prompt + 4 Argon (Medium)** | 保持原版提示词不变，切换为 `gemini-4-argon`（默认 `thinking_level="MEDIUM"`），测试模型升级带来的原生理解与分镜策划提升。 |
| **Tab #3** | **Step 3: Argon 提示词优化器 & Why** | 调用 `gemini-4-argon` 剖析原提示词局限，自动重构适配 Gemini 多模态特性的优化版提示词与逐条 Why 解释；**支持手动直接修改保存提示词**。 |
| **Tab #4** | **Step 4: 优化后Prompt 双模型评测** | 使用优化后的提示词，分别在通道 A (`gemini-3.8-flash`) 与通道 B (`gemini-4-argon`) 上并行评测 10 组样本，验证 Prompt 优化的真实增益。 |
| **Tab #5** | **🎬 5. MV Storyline 创作与词曲对比评测 (V2.2)** | 综合**歌曲音频（歌词、旋律、结构、情绪）+ 人物参考图 (`input_image`) + 用户提示词 (`prompt.txt` 若存在)** 生成完整 MV 故事线（内置五大防坑铁律），并由裁判模型将生成的 Storyline 与原曲逐段对比，输出 **0–100 总分、S1–S6 六大对齐维度分，以及「哪里需要优化、当前问题、为什么、怎么改」**。 |
| **Tab #6** | **🎥 6. MV 全链路自动生成工坊 (V2.3 新增)** | 基于 V2.2 生成的 MV Storyline，支持 **① 一键并行批量生成全部样本 MV** 与 **② 基于单首歌曲/自定义图片上传生成单个 MV**。默认支持 **全曲 100% 等长自动分镜 (`target_duration_sec=0`)**，内置 **三大 MV 演绎与对嘴模式（🎤独唱精准对嘴 `solo_vocal_lipsync` / 🎭合唱对唱分角对嘴 `duet_chorus_lipsync` / 🎬纯故事无需对嘴 `pure_story_cinematic` + 🧠智能声学判别 `auto_detect`）**、**24fps (`41.667ms`) DSP 人声包络 + 男女声部基频 $F_0$ + 词曲音节律动检测**、**Nano Banana 2.1 (`gemini-nano-banana-2.1`) 首帧生成**、**Omni 1.1 Flash (`gemini-omni-1.1-flash-preview`) `[MM:SS.mmm]` 分镜生成**、**毫秒级字幕同步 (`.srt`/`.vtt` + CJK 字幕烧录) 与 FFmpeg 多幕无损合并**。 |
| **Tab #7** | **📊 7. 全景对比、导入导出与快照** | 汇总全部评测轨道的 01–10 得分对比矩阵，支持一键并行跑完全流程、JSON Bundle 导入/导出与历史快照回滚。 |
| **Tab #8** | **⚙️ 8. 配置中心 (Models / Prompts / Storage)** | 独立配置 GCP Project ID、**多源素材存储模式 (`auto / gdrive / gcs / local`)**、**12 个模型角色的独立 `model` / `location` / `thinking_level` / `temperature` / `max_output_tokens` (默认 `65535`)**、**9 个核心 Prompt 在线编辑**，以及**浏览器直接上传样本素材**。 |

### 2.2 V2.3 MV 全链路生成工坊的 6 大精细化音画控制机制

1. **全曲等长自动分镜 (`target_duration_sec = 0` 默认) & 多幕并行渲染**：
   - 自动探测原曲精确时长（如 `183.4s ~ 291.7s`），按 `ceil(song_duration / 10.0)` 自动切分为 `N × 10.0s` 幕，将 V2.2 Storyline 的四幕结构 (`four_act_story_arc`) 与时间锚点 (`time_anchored_story_beats`) 按时间轴映射至每一幕，并在合并时精确裁剪对齐至原曲毫秒级时长。
2. **三大 MV 演绎与对嘴模式 (`mv_performance_mode`) + 智能声学判别 (`auto_detect`)**：
   - **🎤 独唱精准对嘴模式 (`solo_vocal_lipsync`)**：锁定唯一主唱（`LEAD <IMAGE_REF_0>`）在所有人声激活窗口张嘴逐字对嘴演唱，若有配角（`SUPPORT <IMAGE_REF_1>`）同框则强制锁定 `forbidden_silent_acting`（全程闭嘴演戏）。
   - **🎭 合唱/对唱分角对嘴模式 (`duet_chorus_lipsync`)**：根据每个 `[MM:SS.mmm]` 窗口的男/女声基频 ($F_0$) 与双声部和声分布，将词曲逐句分配给 `LEAD`（角色A独唱）、`SUPPORT`（角色B独唱）或 `BOTH`（副歌男女同框合唱），未轮到演唱的角色严格闭嘴互动。
   - **🎬 纯故事叙事模式·无需对嘴 (`pure_story_cinematic`)**：全片所有窗口强制锁定 `is_vocal_open_mouth = False`、`active_speaker = "NONE"`，并自动剥离 Omni `# Transcriptions` 语音块，人物全程自然闭嘴演绎电影级叙事动作，同时利用 24fps 音乐节拍时间戳驱动镜头快切与同步字幕烧录。
3. **24fps (`41.667ms`) DSP 人声包络 + 男女声部基频 ($F_0$) + 词曲音节律动 (`Cadence`) 实测 (`_detect_dsp_vocal_envelope_windows_10s`)**：
   - 对每个 10.0 秒音频分片执行 `220Hz–3600Hz` 人声共振峰带通滤波与 24fps (240 帧) RMS 能量包络计算，自动平滑 `<= 4 帧 (167ms)` 的辅音气口，并对超长乐句（`> 4.2s`）在换气谷值处按 4 帧 (`166.7ms`) 3D Video Latent 边界自动切分；
   - 通过 `8kHz` 降采样自相关基频提取区分 **男声基频带 (`85–178Hz`)**、**女声基频带 (`190–380Hz`)** 与 **双声部和声 (`both`)**，并统计音节包络峰值率 (`syllable_rate_hz`) 映射为 4 类律动口型指令：`slow_sustained_legato`（抒情慢速长音）、`mid_melodic_pop`（中速流行咬字）、`fast_syncopated_staccato`（快切密集说唱/切分）、`instrumental_beat_pulse`（器乐闭嘴卡点）。
4. **声画性别与演唱权限闭环锁 (`Mouth Isolation & Gender Lock`)**：
   - 锁定主角 `<IMAGE_REF_0>[Character A]` 与配角 `<IMAGE_REF_1>[Character B]` 的声部性别与 `singing_permission`；在纯器乐窗口 (`INSTRUMENTAL_CLOSED`) 或非当前演唱角色上强制注入 `ALL MOUTHS TIGHTLY CLOSED, ZERO SPEAKING, LIPS SEALED SHUT`。
5. **Omni 1.1 Flash 原生四段式 Prompt 编译器 (`compile_mv_storyline_omni_prompt_for_act`)**：
   - 严格编译 `# References`、`# Sources`、`# Descriptions`（含 `[MM:SS.mmm]<Video Caption>...<Audio Caption>...`、声部基频 `F0`、音节律动 `rhythm_cadence` 与 `A_ROLL_VOCAL` / `B_ROLL_EMOTION_INSERT` / `C_ROLL_WIDE_CLIMAX` 幕内跳切）与 `# Transcriptions`（内置防语音分类器误拦截自动重试与 24fps 动态运镜分镜保底）。
6. **毫秒级双语字幕同步 (`.srt` / `.vtt` + Pillow RGBA CJK 分批烧录) 与 FFmpeg 全曲无损合并**：
   - 自动导出标准 `.srt` / `.vtt` 字幕文件，并使用内置中文字体通过 Pillow RGBA 渲染 + FFmpeg 多批次滤镜链（每批最多 12 条 overlay，支持全曲 50–100+ 条字幕零溢出）将字幕精准烧录至合并后的完整 MV (`final_mv_subtitled.mp4` 与无字幕纯净版 `final_mv_clean.mp4`)。

### 2.3 标准工程目录结构

```text
mv-eval-studio/
├── main.py                        # 应用启动入口 (CLI & FastAPI ASGI Target)
├── deploy.sh                      # Cloud Run 一键自动化部署脚本 (含 API 启用、SA 授权与三种安全模式)
├── Dockerfile                     # Cloud Run 生产容器镜像构建文件 (python:3.11-slim + ffmpeg + fonts-noto-cjk)
├── requirements.txt               # Python 依赖清单
├── README.md                      # 项目总览与快速导航
├── app/
│   ├── __init__.py
│   ├── config.py                  # 全局配置、模型注册表、Prompt 模板与实时结构化日志
│   ├── storage.py                 # 多源素材解析器 (Local / GCS FUSE / GCS API / Google Drive)
│   ├── evaluation_engine.py       # 音频多模态理解、Prompt 优化器与 Storyline 评测引擎 (Tabs #1-#5)
│   ├── mv_generation_engine.py    # MV 全链路生成工坊：分镜规划、24fps 口型/性别/节奏检测、Omni 1.1 Flash、字幕同步与 FFmpeg 合并 (Tab #6)
│   └── server.py                  # FastAPI 路由与零明文 SHA-256 会话鉴权中间件
├── static/
│   └── index.html                 # 8-Tab 前端单页工作台 (SPA UI)
├── configs/
│   └── samples_manifest.json      # 01-10 基准测试集元数据清单
└── docs/
    ├── QUICKSTART.md              # 快速上手与部署指南
    ├── ARCHITECTURE.md            # 本手册：完整架构、权限、存储与评分白皮书
    └── prompts/                   # 评测基准提示词与规则原文
        ├── audio_analysis_prompt.txt
        ├── dataset_spec.txt
        └── evaluation_rubric.md
```

---

## 三、部署前置条件 (Prerequisites)：需要什么权限？

在部署到 Google Cloud Run 时，涉及 **两类身份** 的权限：**1) 执行部署命令的当前用户（Deployer）** 和 **2) Cloud Run 容器绑定的运行时服务账号（`argon-mv-eval-sa`）**。

### 3.1 当前执行部署的用户 (Deployer) 所需权限

- **快速判断**：如果您在目标 GCP Project 上拥有 **`Owner` (`roles/owner`)**，或同时拥有 **`Editor` (`roles/editor`) + `Project IAM Admin` (`roles/resourcemanager.projectIamAdmin`)**，您已经拥有全部权限，无需额外申请。
- **最小权限表（若您在企业生产项目中不是 Owner，请管理员为您授予以下角色）**：

| 权限分类 | 当前用户 (Deployer) 所需的 IAM 角色 | 用途说明（缺少时的典型报错） |
| :--- | :--- | :--- |
| **① 每次源码构建与部署必选**<br>*(运行 `gcloud run deploy --source .`)* | **`roles/run.admin`** | 创建、更新 Cloud Run 服务，修改环境变量、挂载卷以及配置 `--allow-unauthenticated` / IAM 策略。 |
| ↳ | **`roles/iam.serviceAccountUser`** | 将运行时服务账号 (`argon-mv-eval-sa`) 及 Cloud Build 构建账号绑定 (`actAs`) 到服务与构建任务。 |
| ↳ | **`roles/cloudbuild.builds.editor`** | `--source .` 会触发 Cloud Build 在云端自动根据 `Dockerfile` 构建容器镜像。 |
| ↳ | **`roles/artifactregistry.admin`** | 首次源码部署时自动创建 `cloud-run-source-deploy` 镜像仓库并推送镜像。 |
| ↳ | **`roles/storage.admin`** | 上传源码包至 Cloud Build 暂存桶，以及创建/上传样本 GCS Bucket。 |
| **② 首次初始化必选**<br>*(启用 API + 创建 SA 并给 SA 授权)* | **`roles/serviceusage.serviceUsageAdmin`** | 执行 `gcloud services enable ...` 启用项目 API。 |
| ↳ | **`roles/iam.serviceAccountAdmin`** | 执行 `gcloud iam service-accounts create argon-mv-eval-sa` 创建专用服务账号。 |
| ↳ | **`roles/resourcemanager.projectIamAdmin`** | 执行 `gcloud projects add-iam-policy-binding` 为服务账号授予 `roles/aiplatform.user` 等角色。 |
| **③ 方案三 IAP 可选** | **`roles/iap.admin`** | 仅当采用「方案三：Cloud Run 原生 IAP」且需配置客户邮箱白名单时需要。 |

<details>
<summary><b>📋 点击展开：管理员为当前部署用户 (Deployer) 一键授权的命令</b></summary>

```bash
export PROJECT_ID="genai-dnb-demo"
export DEPLOYER_EMAIL="你的邮箱@example.com"   # 执行部署的工程师账号

for ROLE in \
  "roles/run.admin" \
  "roles/iam.serviceAccountUser" \
  "roles/cloudbuild.builds.editor" \
  "roles/artifactregistry.admin" \
  "roles/storage.admin" \
  "roles/serviceusage.serviceUsageAdmin" \
  "roles/iam.serviceAccountAdmin" \
  "roles/resourcemanager.projectIamAdmin"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="user:${DEPLOYER_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done
```
</details>

---

### 3.2 Cloud Run 运行时服务账号 (`argon-mv-eval-sa`) 必需权限

容器在 Cloud Run 上运行时，通过绑定服务账号 `argon-mv-eval-sa@${PROJECT_ID}.iam.gserviceaccount.com` 免密调用后端服务（**注意：`./deploy.sh` 脚本已自动帮您创建该账号并绑定以下全部权限**）：

| IAM 角色 (Role) / 外部授权 | 权限级别 | 为什么必须配置？（缺少时的报错） |
| :--- | :--- | :--- |
| **`roles/aiplatform.user`**<br>*(Vertex AI User)* | **必需 (Core)** | 调用 Vertex AI 上的 `gemini-3.8-flash` 与 `gemini-4-argon` 模型 (`aiplatform.endpoints.predict`)。 |
| **`roles/serviceusage.serviceUsageConsumer`**<br>*(Service Usage Consumer)* | **必需 (Core)** | 容器通过 ADC 请求 Google Drive API / Vertex AI 时携带 `X-Goog-User-Project` 配额项目头所需的 `serviceusage.services.use` 权限（缺少会报 `403 USER_PROJECT_DENIED`）。 |
| **`roles/logging.logWriter`**<br>*(Logs Writer)* | **必需 (Core)** | 允许 Cloud Run 容器向 Cloud Logging 写入运行日志。 |
| **`roles/storage.objectUser`**<br>*(Storage Object User)* | **GCS 模式必需** | 用于 Cloud Run GCS FUSE 挂载卷或 GCS API 读取 `samples/01..10` 并持久化写入 `runs_v2/*.json` 评测结果。 |
| **Google Drive 文件夹 `Viewer` 共享**<br>+ 启用 `drive.googleapis.com` | **无 GCS 模式必需** | 当样本不在 GCS 上时，允许服务账号通过 Drive v3 API 按需免密下载样本音频与参考图。 |

<details>
<summary><b>📋 点击展开：手动创建 `argon-mv-eval-sa` 并授予上述 4 项权限的命令（`deploy.sh` 已内置）</b></summary>

```bash
export PROJECT_ID="genai-dnb-demo"
export SA_NAME="argon-mv-eval-sa"
export SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. 启用项目必需的 Google Cloud APIs
gcloud services enable \
  aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com drive.googleapis.com storage.googleapis.com \
  --project="${PROJECT_ID}"

# 2. 创建专用服务账号
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="Argon MV Eval Platform Service Account" \
  --project="${PROJECT_ID}" || true

# 3. 批量授予服务账号运行必需的 4 项 IAM 角色
for ROLE in \
  "roles/aiplatform.user" \
  "roles/serviceusage.serviceUsageConsumer" \
  "roles/logging.logWriter" \
  "roles/storage.objectUser"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done
```
</details>

---

## 四、多源素材与存储机制：样本不在（或在）GCS 上如何访问？

在 `argon_mv_eval_platform.py` 中，所有音频、参考图与提示词均通过统一的 `resolve_sample_asset()` 解析。您可以在 **Tab #7 `⚙️ 配置中心`**（或环境变量 `ARGON_ASSET_STORAGE_MODE`）切换 `auto / gdrive / gcs / local` 模式：

### 4.1 场景 A：如果样本【全不在】Google Cloud Storage (GCS) 上
1. **内置元数据与文档自愈**：`samples_manifest.json` 及 3 份规范文档已内嵌于 `argon_mv_eval_platform.py`。即使在只读容器或空目录下启动，也会自动探测可写目录（`./argon_eval_data` 或 `/tmp/argon_eval_data`）并自动释放文档。
2. **Google Drive API 实时按需拉取 + 本地缓存 (`download_drive_file_universal`)**：
   - 当本地缺少样本音频/图片时，自动根据 `samples_manifest.json` 中的 Google Drive `file_id` 按需拉取并缓存在容器内。
   - **Cloud Run 免密拉取配置**：只需打开 Google Drive 样本文件夹（`1MYa7S2ZjTXcOHrK3lDBUzhCVUUHxyp9Z`），点击「共享 (Share)」，将服务账号邮箱 `argon-mv-eval-sa@你的ProjectID.iam.gserviceaccount.com` 添加为 **查看者 (Viewer)** 即可。
3. **浏览器网页端直接上传素材 (`POST /api/v2/samples/upload`)**：
   - 在 **Tab #7 `⚙️ 配置中心` → 顶部卡片底部「📤 浏览器直接上传/替换样本素材」**，可直接从浏览器上传本地 MP3/图片注入容器。
4. **本地完整包直读**：若使用 108.6 MB 完整包部署，10 组样本已位于 `./argon_eval_data/samples/01..10/`，直接以 0 延迟毫秒级读取。

---

### 4.2 场景 B：如果将样本与评测数据放在【GCS Bucket】上（路径与 `REGION` 规范）

#### 1. 三个层面的 `REGION` / `LOCATION` 对照表（切勿混用）
| 组件 | 变量名 / 配置项 | 推荐值 | 关键要求说明 |
| :--- | :--- | :--- | :--- |
| **1. Cloud Run 服务区域** | `GOOGLE_CLOUD_REGION`<br>*(部署脚本中的 `REGION`)* | **`us-central1`**<br>*(或 `asia-east1`)* | ⚠️ **不能填 `global`！** Cloud Run 是区域级服务，必须指定具体的数据中心区域（推荐 `us-central1`）。 |
| **2. GCS Bucket 区域** | 创建 Bucket 时的 Location | **`us-central1`**<br>*(或 Multi-region `US`)* | 💡 **强烈建议与 Cloud Run 保持相同区域 (`us-central1`)**：<br>1) 同区域下 Cloud Run GCS FUSE 挂载读取几十 MB 的 MP3 **速度最快且免收跨区域流量费 (Egress Fee)**；<br>2) Vertex AI 直接读取 `gs://` URI 时，Bucket 在 `us-central1` 或 `US` 兼容性最好。 |
| **3. Vertex AI 模型区域** | `GOOGLE_CLOUD_LOCATION`<br>*(页面配置中心的 `Location`)* | **`global`** | ⚠️ **推荐保持默认 `global`！** `gemini-3.8-flash` 与 `gemini-4-argon` 在 Vertex AI 上的服务端点挂载在 `global` 下。Cloud Run 部署在 `us-central1` 完全可以跨区调用 `location="global"` 的 Vertex AI 模型。 |

#### 2. 创建同区域 GCS Bucket、上传样本并挂载部署的完整命令
```bash
export PROJECT_ID="genai-dnb-demo"
export REGION="us-central1"
export GCS_BUCKET="${PROJECT_ID}-argon-mv-eval"
export SA_EMAIL="argon-mv-eval-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. 在 us-central1 创建与 Cloud Run 同区域的 GCS Bucket
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --uniform-bucket-level-access

# 2. 为 Cloud Run 服务账号授予该 Bucket 的读写权限 (用于读取样本 & 持久化 runs_v2)
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectUser"

# 3A. 【方式 A：配合 deploy.sh 的 GCS FUSE 挂载模式（推荐）】
#     将 argon_eval_data 内部子目录 (samples/, docs/, runs_v2/) 直接上传至 Bucket 根目录：
gcloud storage cp -r argon_eval_data/* "gs://${GCS_BUCKET}/"

# 3B. 【方式 B：若不挂载卷，仅在页面「Tab #7 配置中心」填写 Bucket 名称（默认 Prefix 为 argon_eval_data）】
#     也可将整个 argon_eval_data 文件夹上传（即 gs://${GCS_BUCKET}/argon_eval_data/samples/...）：
gcloud storage cp -r argon_eval_data "gs://${GCS_BUCKET}/"
```

---

## 五、Cloud Run 部署与三种安全访问方案详解

因为普通浏览器直接打开 `https://xxx.run.app` 不会自动携带 GCP IAM 令牌，请根据**访问对象是谁**从以下三种方案中任选一种：

| 方案 | 适用人群 | 安全性 | 便捷度 | 访问者是否需安装 `gcloud`？ | 是否需配置 OAuth Screen？ |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **方案一：`gcloud run services proxy`**<br>*(IAM 零信任本地加密隧道)* | **你自己、内部技术队友** | ⭐⭐⭐⭐⭐<br>*(公网完全 403 拒绝)* | ⭐⭐⭐⭐⭐<br>*(1 条命令秒开)* | ✅ 需要（开发者本机已有） | ❌ **不需要** |
| **方案二：内置零明文 `SHA-256` 门禁**<br>*(掩码密码登录页 / 哈希跳转)* | **发给外部客户直接体验**<br>*(微信/邮件发链接即点即用)* | ⭐⭐⭐⭐<br>*(端到端零明文 + HttpOnly Cookie)* | ⭐⭐⭐⭐⭐<br>*(客户点链接即进)* | ❌ **客户完全不需要** | ❌ **完全不需要！** |
| **方案三：Cloud Run 原生 IAP**<br>*(Identity-Aware Proxy)* | **企业客户长期正式使用**<br>*(使用 Google 账号白名单登录)* | ⭐⭐⭐⭐⭐<br>*(Google 账号白名单)* | ⭐⭐⭐⭐<br>*(浏览器跳转 Google 登录)* | ❌ **客户完全不需要** | 同组织内部不需要；仅外部客户需切为 `External` |

---

### 🟢 方案一：`gcloud run services proxy`（适合你自己或内部团队访问 —— 最安全、零配置）

1. **一键部署**：
   ```bash
   export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
   ./deploy.sh
   ```
2. **（可选）给内部队友授权**：
   ```bash
   gcloud run services add-iam-policy-binding argon-mv-eval-v2 \
     --project="genai-dnb-demo" --region="us-central1" \
     --member="user:teammate@google.com" --role="roles/run.invoker"
   ```
3. **启动本地安全代理并打开浏览器**：
   ```bash
   gcloud run services proxy argon-mv-eval-v2 \
     --project="genai-dnb-demo" --region="us-central1" --port=8095
   ```
   浏览器打开：`http://127.0.0.1:8095`。

---

### 🔵 方案二：内置零明文 `SHA-256` 访问门禁（推荐发链接给外部客户体验 —— 免装 `gcloud`、免配 OAuth Consent）

- **为什么推荐方案二？**
  方案二使用应用内建的 SHA-256 加密门禁中间件，**不经过 Google OAuth 登录，因此完全不需要配置 OAuth Consent Screen**，客户也不需要拥有 Google 账号或安装 `gcloud`。
- **端到端零明文设计**：
  - **部署端零明文 (`ARGON_ACCESS_PASSCODE_SHA256`)**：脚本通过 `read -rsp` 隐藏读取口令，在您本机算出 **64 位不可逆 SHA-256 哈希摘要** 后再传给 Cloud Run。
  - **浏览器端零明文**：客户打开网址时显示暗黑风掩码密码登录卡片（`<input type="password">`），浏览器在前端通过 Web Crypto API 计算 `SHA-256` 后再通过 HTTPS `POST /api/v2/auth/login` 验证，服务端用 `hmac.compare_digest` 比对并种下 7 天有效 `HttpOnly; Secure; SameSite=Lax` Cookie。

1. **一键部署并隐藏输入口令**：
   ```bash
   export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
   PROMPT_PASSCODE="true" ./deploy.sh
   ```
2. **发给客户访问（两种零明文方式）**：
   - **方式 1（推荐：干净网址 + 掩码登录页）**：直接发 `https://argon-mv-eval-v2-xxxxx.us-central1.run.app`，客户输入口令（自动掩码 `••••••`）即可进入，URL 地址栏全程零明文。
   - **方式 2（免输跳转：SHA-256 Token 链接 + 自动 303 抹除地址栏参数）**：发送脚本打印的 `https://xxx.run.app/?token=<64位SHA256哈希>`，客户点击瞬间种下 Cookie 并立即 `303` 重定向回干净的 `/` 根路径，地址栏参数自动消失。
3. **随时更换口令（秒级生效，旧 Cookie 立即失效）**：
   ```bash
   NEW_HASH=$(printf "%s" "新口令" | sha256sum | awk '{print $1}')
   gcloud run services update argon-mv-eval-v2 \
     --project="genai-dnb-demo" --region="us-central1" \
     --update-env-vars="ARGON_ACCESS_PASSCODE_SHA256=${NEW_HASH}"
   ```

---

### 🟣 方案三：Cloud Run 原生集成 IAP（适合企业客户使用 Google 账号白名单登录）

如果您的 GCP 组织策略严禁开启 `--allow-unauthenticated`（导致无法使用方案二），且希望让用户直接用浏览器登录 Google 账号访问，请使用方案三：

1. **关于 OAuth Consent Screen 的配置要求**：
   - **仅同公司/同 GCP Organization 内部同事访问**：无需手动配置 OAuth Consent Screen，Cloud Run 会自动使用 Google 托管的内部认证。
   - **需允许组织外部客户（如 `@gmail.com` 或外部企业邮箱）访问**：请打开 GCP Console → **APIs & Services → OAuth consent screen**，将 User Type 从 `Internal` 切换为 **`External`**，填写应用名称与支持邮箱并保存（仅首次需操作一次，耗时约 1 分钟）。
2. **开启 IAP 一键部署**：
   ```bash
   export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
   ENABLE_IAP="true" ./deploy.sh
   ```
3. **将客户 Google 邮箱加入 IAP 白名单**：
   ```bash
   gcloud iap web add-iam-policy-binding \
     --project="genai-dnb-demo" \
     --resource-type=cloud-run \
     --service="argon-mv-eval-v2" \
     --region="us-central1" \
     --member="user:customer@example.com" \
     --role="roles/iap.httpsResourceAccessor"
   ```

---

## 六、V2.1 / V2.2 评分规则白皮书（可直接发给客户/业务方）

平台内置两套互补的 **LLM-as-a-Judge 锚定式量表评分体系**：

### 6.1 评分体系一：音频理解与 MV 分镜脚本评测（Tab #1 / #2 / #4，三层 13 维度）
依据《MV评判规则.md》，将总分（0–100 分）拆解为三层 13 个子维度（每项 1–5 分）：

| 评估层级 | 权重 | 包含的 13 个子维度 (每项 1-5 分) | 核心考核点 |
| :--- | :--- | :--- | :--- |
| **L1：基础质量与稳定性**<br>*(一票否决底线层)* | **40%** | `L1_1` 人物与形象一致性<br>`L1_2` 画面可用性与生成稳定度<br>`L1_3` 嘴型同步与表演自然度<br>`L1_4` 场景连续性与空间逻辑 | 主角是否锁死在 `1-2` 人、无废片/崩坏幻觉、歌唱段落与器乐段落表演区分准确、空间转换有逻辑。 |
| **L2：音乐驱动的镜头与内容表达**<br>*(核心还原层)* | **35%** | `L2_1` 音乐结构理解与段落推进<br>`L2_2` 节奏卡点与剪辑律动<br>`L2_3` 情绪匹配与叙事递进<br>`L2_4` 镜头语言与运镜调度 | 精确识别 Intro/Verse/Chorus/Bridge/Outro 时间轴、高潮段能量跃升、景别/运镜符合音乐情绪。 |
| **L3：导演审美、创意与传播价值**<br>*(拉开差距层)* | **25%** | `L3_1` 视觉风格统一性<br>`L3_2` 光影构图与色彩审美<br>`L3_3` 概念创意与歌曲定制感<br>`L3_4` 记忆点/高光时刻 (Hero Shots)<br>`L3_5` 整体完成度与回看价值 | 调色光影统一、拒绝泛化万能模板、在高潮处设计出强冲击力的 Hero Shot。 |

- **折算公式**：$\text{Overall}_{100} = \text{round}\Big(\big(0.40 \times \overline{L1} + 0.35 \times \overline{L2} + 0.25 \times \overline{L3}\big) \times 20,\ 1\Big)$
- **一票否决熔断**：若 L1 任意维度 $\le 2$ 分，总分强制封顶 `59.0`；若整首时长幻觉偏差 $>3$ 秒，总分强制封顶 `65.0`。

### 6.2 评分体系二：MV Storyline 创作与原曲对齐评测（Tab #5，V2.2 新增 6 维度 + 诊断）
针对 Tab #5 生成的 MV 故事线，裁判模型会**直接重新聆听原曲 `input_audio_01.mp3` 并查看参考图与 `prompt.txt`**，从 6 个维度（1–5 分）进行对比打分并输出修改处方：
1. **`S1_lyric_imagery` (20%)**：歌词意象提炼与反字面化水平（是否把歌词抽象为电影化隐喻，而非生硬抠字眼画图）。
2. **`S2_melody_rhythm_sync` (20%)**：旋律律动、配器能量与视觉节奏契合度。
3. **`S3_emotional_arc` (20%)**：情绪弧线与词曲反差捕捉（如「欢快旋律唱悲伤歌词」的明暗反差设计）。
4. **`S4_chorus_progression` (15%)**：多遍副歌「同词不同境」的叙事递进（拒绝 Chorus 1 / 2 / 3 机械复制画面）。
5. **`S5_protagonist_prompt_fit` (15%)**：人物参考图气质 & 用户 `prompt.txt` 融合度。
6. **`S6_feasibility_guardrails` (10%)**：物理可拍性与场景收敛度（锁死 1–2 主角与 3–4 个核心场景池）。
- **结构化四要素诊断 (`optimization_diagnostics`)**：自动输出 **① 哪里需要优化 (`target_where`)**、**② 当前问题 (`problem_what`)**、**③ 为什么——基于词曲对比 (`why_from_song_comparison`)**、**④ 具体怎么改 (`how_to_optimize`)**。
