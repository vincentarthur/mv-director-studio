# ⚡ Gemini 4 Argon MV 多模态评测与 Storyline 创作平台 V2.2 — 快速上手指南 (Quickstart Guide)

> **💡 文档定位**：本指南专为希望 **快速完成本地启动或 Cloud Run 部署** 的用户设计，省略底层实现细节。只需根据您的实际使用场景，按顺序复制执行 3 步命令即可完成部署。

---

## 🧭 场景选型建议（先选最适合您的方式）

| 场景选项 | 适用对象 | 是否需要配置 OAuth Consent Screen？ | 访问者是否需安装 `gcloud`？ | 选型建议 |
| :--- | :--- | :--- | :--- | :--- |
| **[场景 A：本地电脑直接运行](#场景-a在本地电脑直接运行30-秒)** | 开发者本机自测 | ❌ **不需要** | 本机需有 Python & `gcloud` | 本地调试首选，30 秒启动。 |
| **[场景 B：Cloud Run + 内置 SHA-256 口令门禁](#场景-b部署到-cloud-run--内置-sha-256-口令门禁推荐外部演示)** | **发链接给外部客户或业务方直接浏览器访问** | ❌ **完全不需要配置 OAuth Consent！**<br>*(走应用内置加密口令验证，不依赖 Google 登录)* | ❌ **不需要** | **⭐ 外部演示最省心推荐**：客户点开链接输入口令即可访问（前提是 GCP 项目未禁止 `--allow-unauthenticated`）。 |
| **[场景 C：Cloud Run + `gcloud proxy` 本地安全隧道](#场景-c部署到-cloud-run--gcloud-proxy-安全隧道推荐内部团队)** | 你自己、内部技术同事 | ❌ **不需要** | ✅ 需要 | **⭐ 内部评测最安全推荐**：公网完全拒绝访问 (`403`)，零额外配置。 |
| **[场景 D：Cloud Run + 原生 IAP 身份代理](#场景-d部署到-cloud-run--原生-iap-身份代理企业级-google-账号白名单)** | 组织策略禁止公开 URL，且需让客户用浏览器直接登录 | ⚠️ **同组织内部访问不需要**；<br>若允许**外部客户**登录，需将 OAuth Consent Screen 设为 `External` (1分钟) | ❌ **不需要** | **⭐ 企业合规推荐**：通过 Google 账号邮箱白名单控制访问权限。 |

---

## 💻 场景 A：在本地电脑直接运行（30 秒）

不需要 Docker，只要本机有 Python 3.10+ 和 `gcloud` 即可：

### 第 1 步：安装依赖
```bash
pip install -r requirements.txt
```

### 第 2 步：登录 Google Cloud 账号并指定项目
*(请将下方的 `genai-dnb-demo` 替换为您自己的 GCP Project ID)*
```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
```

### 第 3 步：启动网页服务
```bash
python3 main.py --host 0.0.0.0 --port 8095
```
✅ **完成！** 打开浏览器访问：**`http://127.0.0.1:8095`**

---

## 🌐 场景 B：部署到 Cloud Run + 内置 SHA-256 口令门禁（推荐外部演示，免配 OAuth）

- **核心优势**：**完全不需要配置 OAuth Consent Screen**，客户也不需要拥有 Google 账号或安装 `gcloud`。客户点开网址后，在内置的加密登录卡片中输入您设定的访问口令即可进入（口令在前端与云端均以不可逆的 `SHA-256` 哈希传输和存储，全程不暴露明文）。

### 第 1 步：设置您的 GCP 项目 ID
*(请将第一行的 `genai-dnb-demo` 替换为您自己的 GCP Project ID)*
```bash
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
export GOOGLE_CLOUD_REGION="us-central1"
gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
```

### 第 2 步：（可选）将 10 首样本上传至同区域 GCS Bucket 持久化保存
*(若您下载的是 108.6 MB 完整包，执行以下 3 行命令会自动创建同区域 GCS 存储桶并上传样本；若暂不使用 GCS，可直接跳至第 3 步)*
```bash
export GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-mv-eval"
gcloud storage buckets create "gs://${GCS_BUCKET}" --project="${GOOGLE_CLOUD_PROJECT}" --location="${GOOGLE_CLOUD_REGION}" --uniform-bucket-level-access || true
gcloud storage cp -r data/* "gs://${GCS_BUCKET}/"
```

### 第 3 步：一键部署并设置访问口令
```bash
chmod +x deploy.sh
PROMPT_PASSCODE="true" ./deploy.sh
```
- 运行后终端会提示：`🔑 请输入要设置的访问口令:`，直接输入您设定的口令并回车（输入过程完全隐藏不回显）。
- 脚本会自动完成：**启用必需 API → 创建服务账号 → 绑定 IAM 权限 → 构建镜像 → 部署至 Cloud Run**。
- ✅ **完成！** 部署结束后，将终端打印出的 `https://argon-mv-eval-v2-xxxx.us-central1.run.app` 网址与口令发送给客户即可。

---

## 🔒 场景 C：部署到 Cloud Run + `gcloud proxy` 安全隧道（推荐内部团队）

此模式下 Cloud Run 开启 `--no-allow-unauthenticated`（公网直接打开网址会返回 `403 Forbidden`），仅允许授权人员通过本地 IAM 加密隧道访问。

### 第 1 步：设置 GCP 项目 ID 并一键部署
*(请将 `genai-dnb-demo` 替换为您自己的 GCP Project ID)*
```bash
export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
export GOOGLE_CLOUD_REGION="us-central1"
chmod +x deploy.sh
./deploy.sh
```

### 第 2 步：在本地建立安全访问代理
```bash
gcloud run services proxy argon-mv-eval-v2 \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${GOOGLE_CLOUD_REGION}" \
  --port=8095
```
✅ **完成！** 保持终端运行，打开浏览器访问：**`http://127.0.0.1:8095`**

---

## 🛡️ 场景 D：部署到 Cloud Run + 原生 IAP 身份代理（企业级 Google 账号白名单）

如果您的企业 GCP 组织策略禁止开启 `--allow-unauthenticated`（因此无法使用场景 B），同时希望用户直接用浏览器通过 Google 账号登录访问，推荐使用 Cloud Run 原生 IAP：

1. **关于 OAuth Consent Screen 的说明**：
   - 如果只允许**同公司内部（同一 GCP Organization 域名下）**的同事访问：**无需手动配置 OAuth Consent Screen**。
   - 如果需要允许**外部客户（如 `@gmail.com` 或外部企业邮箱）**访问：请在 GCP Console → **APIs & Services → OAuth consent screen** 中将 User Type 设为 **`External`**，填写应用名称与支持邮箱并保存（仅首次需操作一次，耗时约 1 分钟）。
2. **第 1 步：开启 IAP 一键部署**
   ```bash
   export GOOGLE_CLOUD_PROJECT="genai-dnb-demo"
   export GOOGLE_CLOUD_REGION="us-central1"
   chmod +x deploy.sh
   ENABLE_IAP="true" ./deploy.sh
   ```
3. **第 2 步：将允许访问的 Google 邮箱加入 IAP 白名单**
   ```bash
   gcloud iap web add-iam-policy-binding \
     --project="${GOOGLE_CLOUD_PROJECT}" \
     --resource-type=cloud-run \
     --service="argon-mv-eval-v2" \
     --region="${GOOGLE_CLOUD_REGION}" \
     --member="user:authorized-user@example.com" \
     --role="roles/iap.httpsResourceAccessor"
   ```
   ✅ **完成！** 白名单内的用户直接打开 Cloud Run 网址并登录 Google 账号即可访问。

---

## 🚑 常见问题快速排查 (Troubleshooting)

### 问题 1：运行 `./deploy.sh` 时提示 `PERMISSION_DENIED`
说明您当前登录的个人账号在该 GCP Project 中缺少部署权限。请将以下命令发送给该项目的 **GCP 管理员（Owner）**，将 `DEPLOYER_EMAIL` 替换为您的邮箱后执行一次即可：

```bash
export PROJECT_ID="genai-dnb-demo"           # 替换为目标 Project ID
export DEPLOYER_EMAIL="你的邮箱@example.com"  # 替换为执行部署的工程师邮箱

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
    --member="user:${DEPLOYER_EMAIL}" --role="${ROLE}" --condition=None --quiet
done
```

### 问题 2：使用 170 KB 轻量包且未配置 GCS 时，如何加载样本音频和图片？
任选以下一种方式即可：
1. **方式 1（使用完整包）**：直接下载解压 **108.6 MB 完整包 (`mv-eval-studio-v2.3.0-full.zip`)** 后再部署，10 组无损样本会直接内置于容器中。
2. **方式 2（网页直接上传）**：打开平台网页 → 点击顶部 **Tab #7 `⚙️ 配置中心`** → 在第一张卡片底部的 **「📤 浏览器直接上传/替换样本素材」** 直接选择本地音频/图片上传。
3. **方式 3（Google Drive 免密按需拉取）**：打开 Google Drive 样本文件夹（`1MYa7S2ZjTXcOHrK3lDBUzhCVUUHxyp9Z`），点击「共享 (Share)」，将服务账号邮箱 `argon-mv-eval-sa@你的ProjectID.iam.gserviceaccount.com` 添加为查看者（Viewer）。

---
> 📚 如需查阅 **7-Tab 完整功能架构、IAM 权限明细表、多源存储机制、以及面向客户的 13维/6维评分规则白皮书**，请打开同目录下的 **[`docs/ARCHITECTURE.md`](./ARCHITECTURE.md)**。
