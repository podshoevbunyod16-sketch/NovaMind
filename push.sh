#!/bin/bash
# Скрипт для отправки изменений в репозиторий
# Использование:
#   1) export GITHUB_TOKEN=ghp_xxx (с правами repo)
#   2) ./push.sh
# Или используй SSH: git remote set-url origin git@github.com:podshoevbunyod16-sketch/NovaMind.git

set -e

BRANCH="${1:-main}"

if [ -z "$GITHUB_TOKEN" ]; then
    echo "❌ GITHUB_TOKEN не задан. Экспортируй его перед запуском:"
    echo "   export GITHUB_TOKEN=ghp_xxx"
    exit 1
fi

echo "📦 Коммичу изменения..."
git add -A
if git diff --cached --quiet; then
    echo "ℹ️  Нет изменений для коммита"
else
    MSG="${2:-Update: $(date '+%Y-%m-%d %H:%M:%S')}"
    git commit -m "$MSG"
fi

echo "🚀 Пушинг в origin/$BRANCH..."
git push "https://x-access-token:$GITHUB_TOKEN@github.com/podshoevbunyod16-sketch/NovaMind.git" "$BRANCH"
echo "✅ Готово!"