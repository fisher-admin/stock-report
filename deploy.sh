#!/bin/bash

echo "=================================="
echo "📊 A股选股报告部署工具"
echo "=================================="

# 检查是否安装了 Git
if ! command -v git &> /dev/null; then
    echo "❌ Git 未安装，请先安装 Git"
    exit 1
fi

# 检查是否是 Git 仓库
if [ ! -d .git ]; then
    echo "📦 初始化 Git 仓库..."
    git init
fi

# 添加所有文件
echo "📝 添加文件..."
git add .

# 用户输入仓库信息
echo ""
echo "请提供以下信息:"
read -p "GitHub 用户名: " github_username
read -p "仓库名称 (默认: stock-report): " repo_name

if [ -z "$repo_name" ]; then
    repo_name="stock-report"
fi

# 提交
echo ""
read -p "提交信息 (默认: Update stock report): " commit_msg

if [ -z "$commit_msg" ]; then
    commit_msg="Update stock report: $(date '+%Y-%m-%d %H:%M')"
fi

git commit -m "$commit_msg"

# 设置远程仓库
remote_url="https://github.com/$github_username/$repo_name.git"
echo "🔗 设置远程仓库: $remote_url"
git remote add origin $remote_url 2>/dev/null || git remote set-url origin $remote_url

# 推送到 main 分支
echo "🚀 推送到 GitHub..."
git push -u origin main

echo ""
echo "=================================="
echo "✅ 部署完成!"
echo "=================================="
echo ""
echo "下一步:"
echo "1. 访问 https://github.com/$github_username/$repo_name/settings/pages"
echo "2. 在 'Source' 部分选择 'Deploy from a branch'"
echo "3. Branch 选择 'gh-pages' / '(root)'"
echo "4. 点击 'Save'"
echo ""
echo "你的页面将在以下地址:"
echo "https://$github_username.github.io/$repo_name/"
