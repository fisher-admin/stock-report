# A股选股分析报告

📊 A股综合选股分析报告可视化页面

## 📌 项目简介

本项目提供A股选股分析报告的可视化展示，包括：
- 大盘环境分析
- 综合评分系统
- TOP 20 推荐股票
- 详细因子分析
- 仓位控制建议

## 🚀 部署到 GitHub Pages

### 方法一：通过 GitHub 上传（推荐）

1. **登录 GitHub**
   - 访问 https://github.com
   - 登录你的账号

2. **创建新仓库**
   - 点击右上角 "+" → "New repository"
   - Repository name: `stock-report`
   - 选择 "Public"
   - 不要勾选 "Add a README file"
   - 点击 "Create repository"

3. **上传文件**
   - 在仓库页面，点击 "uploading an existing file"
   - 将 `stock-report` 文件夹中的所有文件拖拽上传
   - 包括：
     - `index.html`
     - `css/style.css`
     - `js/app.js`
     - `js/chart.min.js`
     - `.github/workflows/deploy.yml`

4. **启用 GitHub Pages**
   - 进入仓库的 "Settings" → "Pages"
   - Source 选择 "Deploy from a branch"
   - Branch 选择 "gh-pages" / "(root)"
   - 点击 "Save"
   - 等待 1-2 分钟部署完成

5. **访问你的页面**
   - 地址格式：`https://你的用户名.github.io/stock-report/`

### 方法二：通过 Git 命令行

```bash
# 进入项目目录
cd stock-report

# 初始化 Git 仓库
git init
git add .
git commit -m "Initial commit: A股选股分析报告"

# 创建 GitHub 仓库（通过网页）
# 然后连接远程仓库
git remote add origin https://github.com/你的用户名/stock-report.git

# 推送并创建 gh-pages 分支
git push -u origin main
```

## 📊 数据来源

- 数据来源：AkShare (东方财富)
- 更新时间：2026年2月11日

## ⚠️ 免责声明

本项目仅供学习和研究参考，不构成任何投资建议。投资者应根据自身风险承受能力谨慎决策。

## 📝 License

MIT License
