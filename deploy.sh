#!/usr/bin/env bash
# ==============================================================================
# 一键安全极速部署 MV 评测与 Storyline 平台 V2.2 到 Google Cloud Run
# 自动完成：启用必需 API -> 创建最小权限服务账号 -> 绑定 IAM 权限 -> 构建并部署 Cloud Run
# 支持三种安全访问模式：
#   1. 默认模式 (IAM 零信任私密模式):
#      ./deploy.sh
#   2. 零明文 SHA-256 口令门禁模式 (本地交互式隐藏输入口令，云端仅存 64 位 SHA-256 哈希，客户免装 gcloud):
#      PROMPT_PASSCODE="true" ./deploy.sh
#      (或直接传入哈希: ARGON_ACCESS_PASSCODE_SHA256="<64位sha256>" ./deploy.sh)
#   3. 企业级原生 IAP 模式 (Google 账号白名单):
#      ENABLE_IAP="true" ./deploy.sh
# ==============================================================================
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-genai-dnb-demo}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-mv-director-studio-v2}"
SA_NAME="${SA_NAME:-mv-director-studio-sa}"
SA_EMAIL="${SA_EMAIL:-${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com}"
# 可选：若将素材与持久化数据放在 GCS Bucket，填写 Bucket 名称（同时兼容 GCS_BUCKET 与 ARGON_GCS_BUCKET 两种写法！）
GCS_BUCKET="${GCS_BUCKET:-${ARGON_GCS_BUCKET:-}}"
# 可选：零明文 SHA-256 门禁模式（支持交互式隐藏输入 PROMPT_PASSCODE=true 或直接传 SHA-256 哈希）
PROMPT_PASSCODE="${PROMPT_PASSCODE:-false}"
PASSCODE_SHA256="${ARGON_ACCESS_PASSCODE_SHA256:-}"
RAW_PASSCODE="${ARGON_ACCESS_PASSCODE:-}"
# 可选：若希望开启 Cloud Run 原生 IAP (Identity-Aware Proxy)
ENABLE_IAP="${ENABLE_IAP:-false}"
# 可选：是否跳过自动创建与授权 Service Account (默认 false，即自动配置权限)
SKIP_IAM_SETUP="${SKIP_IAM_SETUP:-false}"

if [[ "${PROMPT_PASSCODE}" == "true" && -z "${PASSCODE_SHA256}" && -z "${RAW_PASSCODE}" ]]; then
  read -rsp "🔑 请输入要设置的访问口令 (输入过程完全隐藏，不留存历史记录): " RAW_PASSCODE
  echo ""
fi

if [[ -n "${RAW_PASSCODE}" && -z "${PASSCODE_SHA256}" ]]; then
  # 在本地直接计算不可逆的 SHA-256 摘要（兼容 Linux sha256sum 与 macOS shasum -a 256）
  if command -v sha256sum >/dev/null 2>&1; then
    PASSCODE_SHA256=$(printf "%s" "${RAW_PASSCODE}" | sha256sum | awk '{print $1}')
  else
    PASSCODE_SHA256=$(printf "%s" "${RAW_PASSCODE}" | shasum -a 256 | awk '{print $1}')
  fi
  unset RAW_PASSCODE
fi

echo "🚀 部署项目: ${PROJECT_ID} | 区域: ${REGION} | 服务名: ${SERVICE_NAME} | 服务账号: ${SA_EMAIL} | GCS Bucket: ${GCS_BUCKET:-<未指定>}"

if [[ "${SKIP_IAM_SETUP}" != "true" ]]; then
  echo "🔧 [1/3] 正在检查并启用必需的 Google Cloud APIs..."
  gcloud services enable \
    aiplatform.googleapis.com \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    drive.googleapis.com \
    storage.googleapis.com \
    --project="${PROJECT_ID}" --quiet || true

  echo "👤 [2/3] 正在检查并配置专用服务账号 (${SA_EMAIL}) 及必需 IAM 权限..."
  if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${SA_NAME}" \
      --display-name="Argon MV Eval Platform Service Account" \
      --project="${PROJECT_ID}" --quiet
  fi

  for ROLE in \
    "roles/aiplatform.user" \
    "roles/serviceusage.serviceUsageConsumer" \
    "roles/logging.logWriter" \
    "roles/storage.objectUser"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA_EMAIL}" \
      --role="${ROLE}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done
fi

if [[ -n "${GCS_BUCKET}" ]]; then
  echo "🪣 正在确保服务账号 (${SA_EMAIL}) 拥有 gs://${GCS_BUCKET} 的读写权限 (roles/storage.objectUser)..."
  gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectUser" \
    --quiet >/dev/null 2>&1 || true
fi

echo "📦 [3/3] 正在构建并部署 Cloud Run 服务 (${SERVICE_NAME})..."

# 统一拼接所有环境变量到单个 --set-env-vars 中（gcloud 不允许同时传入 --set-env-vars 和 --update-env-vars）
ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,ARGON_ASSET_STORAGE_MODE=auto"

DEPLOY_ARGS=(
  run deploy "${SERVICE_NAME}"
  --source .
  --project "${PROJECT_ID}"
  --region "${REGION}"
  --service-account "${SA_EMAIL}"
  --execution-environment gen2
  --cpu 4
  --memory 8Gi
  --no-cpu-throttling
  --timeout 1800
  --concurrency 20
  --min-instances 0
  --max-instances 1
)

if [[ -n "${PASSCODE_SHA256}" ]]; then
  echo "🔐 启用【方案二：零明文 SHA-256 登录门禁模式】— 云端仅存储 SHA-256 哈希 (${PASSCODE_SHA256:0:12}...)"
  DEPLOY_ARGS+=(--allow-unauthenticated)
  ENV_VARS="${ENV_VARS},ARGON_ACCESS_PASSCODE_SHA256=${PASSCODE_SHA256}"
else
  echo "🔒 启用【私密 IAM 零信任边界 (--no-allow-unauthenticated)】"
  DEPLOY_ARGS+=(--no-allow-unauthenticated)
fi

if [[ -n "${GCS_BUCKET}" ]]; then
  echo "☁️ 启用 Cloud Run 原生 GCS FUSE 持久化挂载: gs://${GCS_BUCKET}"
  DEPLOY_ARGS+=(
    --add-volume "name=eval-data,type=cloud-storage,bucket=${GCS_BUCKET}"
    --add-volume-mount "volume=eval-data,mount-path=/app/argon_eval_data"
  )
  ENV_VARS="${ENV_VARS},ARGON_GCS_BUCKET=${GCS_BUCKET},ARGON_EVAL_DATA_DIR=/app/argon_eval_data"
fi

DEPLOY_ARGS+=(--set-env-vars "${ENV_VARS}")

gcloud "${DEPLOY_ARGS[@]}"

if [[ "${ENABLE_IAP}" == "true" ]]; then
  echo "🛡️ 正在开启【方案三：Cloud Run 原生 IAP (Identity-Aware Proxy)】..."
  gcloud beta run services update "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --no-allow-unauthenticated \
    --iap
fi

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || echo "https://${SERVICE_NAME}-xxxx.${REGION}.run.app")

echo ""
echo "=============================================================================="
echo "✅ 部署完成！服务地址: ${SERVICE_URL}"
if [[ -n "${PASSCODE_SHA256}" ]]; then
  echo "👉 【方案二：零明文 SHA-256 门禁已开启】"
  echo "   1. 客户直接打开干净网址（进入掩码密码登录页，前端 SHA-256 哈希后 POST 验证，URL 零明文）："
  echo "      ${SERVICE_URL}"
  echo "   2. 或发送带 SHA-256 Token 的免输跳转链接（打开后自动种下 HttpOnly Cookie 并 303 抹除地址栏参数）："
  echo "      ${SERVICE_URL}/?token=${PASSCODE_SHA256}"
elif [[ "${ENABLE_IAP}" == "true" ]]; then
  echo "👉 【方案三：IAP Google 账号白名单访问】请确认已在 IAP 中授权客户邮箱后直接打开："
  echo "   ${SERVICE_URL}"
else
  echo "👉 【方案一：IAM 本地安全隧道访问】请在终端运行以下命令："
  echo "   gcloud run services proxy ${SERVICE_NAME} --project=${PROJECT_ID} --region=${REGION} --port=8095"
  echo "   然后浏览器打开: http://127.0.0.1:8095"
fi
echo "=============================================================================="
