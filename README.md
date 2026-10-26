# 🎵 Gemini 3.8 Flash & 4 Argon — MV 多模态评测与 Storyline 创作平台 (V2.2)

欢迎使用 **Gemini 3.8 Flash (`gemini-3.8-flash`)** 与 **Gemini 4 Argon (`gemini-4-argon`)** 多模态 MV 评测与 Storyline 创作平台（Version 2.2）。

为了兼顾 **快速部署上手** 与 **深度架构/权限/评分规则查阅**，本仓库提供两份独立命名的说明文档，请按需打开：

| 文档文件名 | 适用读者 | 内容说明 |
| :--- | :--- | :--- |
| 👉 **[`docs/QUICKSTART.md`](./docs/QUICKSTART.md)** | **⚡ 希望快速跑通部署的用户** | **推荐优先阅读！** 提供清晰的场景选型建议，分「本地运行 / 外部客户口令门禁部署 / 内部团队隧道部署 / 企业 IAP 部署」4 个场景，按顺序复制执行 3 步命令即可完成。 |
| 👉 **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)** | **📘 架构师 / 运维 / 业务评测人员** | 包含完整的 **7-Tab 架构说明、Deployer 与 Service Account 权限前置清单、无 GCS / 有 GCS 多源素材机制、三种安全访问模式详解、以及 13维/6维评分规则白皮书**。 |

---

## 🧭 核心选型建议（关于外部访问与 OAuth Consent Screen 的说明）

- **如果您需要发链接给【外部客户】直接用浏览器打开体验（最推荐：场景 B / 方案二）**：
  - 使用应用内置的 **零明文 `SHA-256` 口令门禁模式 (`PROMPT_PASSCODE="true"`)**。
  - **❓ 需要配置 OAuth Consent Screen 吗？** 👉 **完全不需要！** 因为该模式由应用自身渲染加密口令登录页（不走 Google OAuth 登录），客户既不需要安装 `gcloud`，也不需要拥有 Google 账号，点开网址输入口令即可访问（仅要求您的 GCP 项目未通过组织策略禁止 `--allow-unauthenticated`）。
- **如果您的企业 GCP 组织策略严禁开启 `--allow-unauthenticated`**：
  - **仅供自己或内部同事使用** 👉 选择 **场景 C / 方案一 (`gcloud run services proxy`)**，公网完全拦截 (`403`)，本地一条命令建立加密隧道，无需配置 OAuth Consent Screen。
  - **需让用户直接用浏览器登录 Google 账号访问** 👉 选择 **场景 D / 方案三 (`Cloud Run 原生 IAP`)**。同公司内部员工访问无需手动配置 OAuth Screen；仅当允许组织外部的客户邮箱登录时，需在 GCP Console 将 OAuth Consent Screen 设为 `External`（耗时约 1 分钟）。

---

## 🔐 Prerequisites（Cloud Run 部署与运行前置权限要求）

将本应用通过 [`deploy.sh`](./deploy.sh) 部署到 Cloud Run 涉及两类账号权限：**1) 执行部署命令的账号（Deployer Account）** 与 **2) Cloud Run 容器运行时服务账号（Runtime Service Account）**。

### 1. 执行部署的账号（Deployer Account，即运行 `./deploy.sh` 的个人或 CI/CD 账号）

> **💡 快捷判断**：如果您的账号在目标 GCP 项目上拥有 **`Owner` (`roles/owner`)**，或者同时拥有 **`Editor` (`roles/editor`) + `Project IAM Admin` (`roles/resourcemanager.projectIamAdmin`)**，则已包含全部所需权限，可直接执行 `./deploy.sh`。

若按最小权限原则（Least Privilege）授权，[`deploy.sh`](./deploy.sh) 各阶段所需 IAM 角色如下：

#### A. 核心构建与部署权限（每次运行 `gcloud run deploy --source .` 必选，共 5 个角色）
| IAM Role ID | 角色名称 | 在 `deploy.sh` 中的具体用途 |
| :--- | :--- | :--- |
| `roles/run.admin` | Cloud Run Admin | 创建/更新 Cloud Run 服务、配置 `4 vCPU / 8Gi` 规格、挂载 GCS FUSE 卷，以及设置 `--allow-unauthenticated` 或 IAM 策略 |
| `roles/iam.serviceAccountUser` | Service Account User | 将运行时服务账号 (`argon-mv-eval-sa`) 及 Cloud Build 默认构建服务账号绑定 (`actAs`) 到 Cloud Run 服务上 |
| `roles/cloudbuild.builds.editor` | Cloud Build Editor | `--source .` 源码部署会触发 Cloud Build 根据 `Dockerfile` 在云端构建容器镜像 |
| `roles/artifactregistry.admin` | Artifact Registry Administrator | 首次 `--source .` 部署时自动创建 `cloud-run-source-deploy` 镜像仓库并推送容器镜像（若仓库已存在，仅需 `roles/artifactregistry.writer`） |
| `roles/storage.admin` | Storage Admin | 将本地源码打包上传至 `gs://<PROJECT_ID>_cloudbuild` 构建暂存桶，并在指定 `GCS_BUCKET` 时为运行时服务账号授予 Bucket 权限 |

#### B. 首次环境初始化权限（默认 `SKIP_IAM_SETUP="false"` 时必选，共 3 个角色）
首次执行 `./deploy.sh` 时，脚本会自动启用 GCP API、创建专用运行时服务账号 `argon-mv-eval-sa` 并为其绑定权限：
| IAM Role ID | 角色名称 | 在 `deploy.sh` 中的具体用途 |
| :--- | :--- | :--- |
| `roles/serviceusage.serviceUsageAdmin` | Service Usage Admin | 执行 `gcloud services enable` 启用 `aiplatform`、`run`、`cloudbuild`、`artifactregistry`、`drive`、`storage` API |
| `roles/iam.serviceAccountAdmin` | Service Account Admin | 创建运行时服务账号 `argon-mv-eval-sa` |
| `roles/resourcemanager.projectIamAdmin` | Project IAM Admin | 执行 `gcloud projects add-iam-policy-binding` 为运行时服务账号授予 Vertex AI / Logging / Storage 角色 |

*注：若项目管理员已预先创建好 `argon-mv-eval-sa` 并完成授权，部署者只需执行 `SKIP_IAM_SETUP="true" ./deploy.sh`，即可省去上述 **B 组**的 3 个管理权限。*

#### C. 可选权限（仅当启用 `ENABLE_IAP="true"` 原生 Google IAP 模式时需要）
- `roles/iap.admin` (*IAP-secured Web App Admin*)：配置 Cloud Run 原生 IAP 访问白名单。

### 2. Cloud Run 容器运行时服务账号权限（`argon-mv-eval-sa`）

[`deploy.sh`](./deploy.sh) 会自动创建 `argon-mv-eval-sa@${PROJECT_ID}.iam.gserviceaccount.com` 作为容器运行时的身份，并自动绑定以下 **4 个角色**：
1. **`roles/aiplatform.user`** (*Vertex AI User*)：调用 `gemini-4-argon` 与 `gemini-3.8-flash` 模型。
2. **`roles/serviceusage.serviceUsageConsumer`** (*Service Usage Consumer*)：提供 `serviceusage.services.use` 权限，支持请求携带 `x-goog-user-project` Header 进行配额计费。
3. **`roles/logging.logWriter`** (*Logs Writer*)：将容器实时流水线日志写入 Cloud Logging。
4. **`roles/storage.objectUser`** (*Storage Object User*)：读写 GCS Bucket（用于 GCS FUSE 持久化挂载卷及评测 JSON 产物归档）。

### 3. 给部署账号一键授权脚本（供项目管理员执行）

```bash
PROJECT_ID="您的GCP项目ID"
DEPLOYER_MEMBER="user:您的邮箱@google.com"  # 若为服务账号则改为 serviceAccount:xxx@...

for ROLE in \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/iam.serviceAccountAdmin \
  roles/resourcemanager.projectIamAdmin \
  roles/serviceusage.serviceUsageAdmin \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.admin \
  roles/storage.admin; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${DEPLOYER_MEMBER}" \
    --role="${ROLE}"
done
```

---

## ⚡ 快速执行命令速查（摘自快速上手指南）

### 场景 A：在本地电脑直接运行（30 秒）
```bash
pip install -r requirements.txt
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"   # 替换为您的 Project ID
python3 main.py --host 0.0.0.0 --port 8095
# 浏览器打开: http://127.0.0.1:8095
```

### 场景 B：部署到 Cloud Run 发链接给【外部客户】直接访问（免装 `gcloud`、免配 OAuth Consent、零明文 SHA-256 口令保护）
```bash
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"   # 替换为您的 Project ID
export GOOGLE_CLOUD_REGION="us-central1"
chmod +x deploy.sh
PROMPT_PASSCODE="true" ./deploy.sh
# 按终端提示输入您设定的访问口令（屏幕不回显，云端仅存 SHA-256 哈希），部署完成后将输出的网址和口令发给客户即可
```

### 场景 C：部署到 Cloud Run 仅供【自己或内部队友】访问（公网 403 私密模式）
```bash
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"   # 替换为您的 Project ID
export GOOGLE_CLOUD_REGION="us-central1"
chmod +x deploy.sh
./deploy.sh

# 部署完成后，在本地启动 IAM 安全加密隧道并打开 http://127.0.0.1:8095：
gcloud run services proxy argon-mv-eval-v2 --project="${GOOGLE_CLOUD_PROJECT}" --region="${GOOGLE_CLOUD_REGION}" --port=8095
```

---
> 📖 **更多内容**：
> - 遇到 `PERMISSION_DENIED` 如何让管理员一键授权？👉 请参阅 [`docs/QUICKSTART.md`](./docs/QUICKSTART.md) 底部的「常见问题快速排查」。
> - 查看 **Deployer 与 Service Account 完整权限表、GCS 建桶与 Region 规范、IAP 配置、以及 V2.1/V2.2 评分规则白皮书** 👉 请打开 [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)。
