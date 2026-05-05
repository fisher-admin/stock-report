#!/usr/bin/env python3
"""
GitHub Pages 数据生成器
=====================

功能：
1. 读取选股结果和 AI 分析数据
2. 生成情绪因子报告
3. 更新 data.json 供 GitHub Pages 使用
4. 自动生成部署脚本

使用：
  python3 generate_github_pages.py              # 生成数据
  python3 generate_github_pages.py --deploy     # 生成并部署
"""

import os
import sys
import json
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

# 配置
STOCK_DATA_ROOT = Path.home() / ".openclaw" / "workspace" / "stock_data"
REPORT_REPO = STOCK_DATA_ROOT / "03-working" / "stock-report-repo"
AI_ANALYSIS_DIR = STOCK_DATA_ROOT / "03-working" / "ai_analysis"
SENTIMENT_DIR = STOCK_DATA_ROOT / "03-working" / "sentiment_factors"
SELECTION_HISTORY_DIR = STOCK_DATA_ROOT / "03-working" / "selection_history"

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """加载 JSON 文件"""
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载 {path} 失败: {e}")
        return None


def get_latest_trade_date() -> str:
    """获取最新交易日期"""
    today = datetime.now()
    # 如果是周末，回退到周五
    weekday = today.weekday()
    if weekday == 5:  # 周六
        today -= timedelta(days=1)
    elif weekday == 6:  # 周日
        today -= timedelta(days=2)
    return today.strftime('%Y%m%d')


def load_ai_analysis(date: str) -> List[Dict[str, Any]]:
    """加载 AI 分析结果"""
    json_file = AI_ANALYSIS_DIR / f"{date}.json"
    if json_file.exists():
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载 AI 分析失败: {e}")
    return []


def calculate_sentiment_summary(days: int = 30) -> Dict[str, Any]:
    """计算情绪因子摘要"""
    today = datetime.now()
    all_sentiments = []
    
    for i in range(days):
        date = (today - timedelta(days=i)).strftime('%Y%m%d')
        data = load_ai_analysis(date)
        
        for item in data:
            all_sentiments.append({
                'date': date,
                'sentiment_score': item.get('sentiment_score', 50),
                'trend_prediction': item.get('trend_prediction', ''),
                'decision_type': item.get('decision_type', '')
            })
    
    if not all_sentiments:
        return {
            'data_points': 0,
            'market_sentiment_avg': 50,
            'market_trend': '无数据',
            'buy_ratio': 0,
            'sell_ratio': 0,
            'hold_ratio': 0
        }
    
    scores = [s['sentiment_score'] for s in all_sentiments]
    market_avg = sum(scores) / len(scores)
    
    total = len(all_sentiments)
    buy_count = sum(1 for s in all_sentiments if s['decision_type'] == 'buy')
    sell_count = sum(1 for s in all_sentiments if s['decision_type'] == 'sell')
    hold_count = sum(1 for s in all_sentiments if s['decision_type'] == 'hold')
    
    return {
        'data_points': total,
        'market_sentiment_avg': round(market_avg, 2),
        'market_trend': '看多' if market_avg > 60 else '看空' if market_avg < 40 else '震荡',
        'buy_ratio': round(buy_count / total * 100, 2),
        'sell_ratio': round(sell_count / total * 100, 2),
        'hold_ratio': round(hold_count / total * 100, 2)
    }


def merge_ai_data_to_top20(top20: List[Dict], ai_data: List[Dict]) -> List[Dict]:
    """将 AI 分析数据合并到 top20"""
    ai_map = {}
    for item in ai_data:
        code = item.get('code', '')
        if '.' in code:
            code = code.split('.')[0]
        ai_map[code] = item
    
    result = []
    for stock in top20:
        code = stock.get('code', '')
        if '.' in code:
            code = code.split('.')[0]
        
        if code in ai_map:
            ai = ai_map[code]
            stock['ai_score'] = ai.get('sentiment_score', 50)
            stock['ai_advice'] = ai.get('operation_advice', '')
            stock['ai_decision'] = ai.get('decision_type', '')
            stock['ai_confidence'] = ai.get('confidence_level', '')
            stock['ai_summary'] = ai.get('dashboard', {}).get('core_conclusion', {}).get('one_sentence', '')
            stock['ai_status'] = 'ready'
        else:
            stock['ai_status'] = 'pending'
        
        result.append(stock)
    
    return result


def generate_pages_data(date: str = None) -> Dict[str, Any]:
    """生成 GitHub Pages 数据"""
    if date is None:
        date = get_latest_trade_date()
    
    logger.info(f"生成数据日期: {date}")
    
    # 加载现有 data.json
    existing_data = load_json_file(REPORT_REPO / "data.json") or {}
    
    # 加载 AI 分析
    ai_data = load_ai_analysis(date)
    logger.info(f"AI 分析数据: {len(ai_data)} 条")
    
    # 计算情绪摘要
    sentiment = calculate_sentiment_summary(30)
    logger.info(f"情绪因子: 平均 {sentiment['market_sentiment_avg']}, 趋势 {sentiment['market_trend']}")
    
    # 合并数据
    top20 = existing_data.get('top20', [])
    if top20 and ai_data:
        top20 = merge_ai_data_to_top20(top20, ai_data)
    
    # 生成新数据
    pages_data = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'latest_trade_date': date,
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_stocks': existing_data.get('total_stocks', 0),
        'data_source': existing_data.get('data_source', 'Tushare Pro'),
        'strategy': existing_data.get('strategy', '启动前夕 v4.1'),
        'factor_weights': existing_data.get('factor_weights', {}),
        'backtest': existing_data.get('backtest', {}),
        'has_ai_analysis': len(ai_data) > 0,
        'ai_analyzed_count': len(ai_data),
        'count': len(top20),
        'top20': top20,
        'sentiment_summary': sentiment
    }
    
    return pages_data


def save_pages_data(data: Dict[str, Any]):
    """保存数据到 GitHub Pages 仓库"""
    # 保存 data.json
    output_file = REPORT_REPO / "data.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {output_file}")
    
    # 保存 combined.json (兼容旧格式)
    combined_file = REPORT_REPO / "combined.json"
    with open(combined_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {combined_file}")


def generate_sentiment_page():
    """生成情绪因子页面"""
    sentiment = calculate_sentiment_summary(30)
    
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>情绪因子报告</title>
    <script src="https://cdn.jsdelivr.net/npm/vue@3"></script>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2/dist/tailwind.min.css" rel="stylesheet">
    <style>
        .metric-card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .trend-up {{ color: #16a34a; }}
        .trend-down {{ color: #dc2626; }}
        .trend-neutral {{ color: #6b7280; }}
    </style>
</head>
<body class="bg-gray-50">
<div id="app" class="max-w-6xl mx-auto p-6">
    <header class="mb-8">
        <h1 class="text-3xl font-bold text-gray-800">📊 情绪因子报告</h1>
        <p class="text-gray-600 mt-2">过去 30 天市场情绪分析</p>
    </header>
    
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <div class="metric-card">
            <h3 class="text-sm font-semibold text-gray-500">市场情绪</h3>
            <p class="text-3xl font-bold mt-2 {sentiment['market_trend'] == '看多' and 'trend-up' or sentiment['market_trend'] == '看空' and 'trend-down' or 'trend-neutral'}">
                {sentiment['market_sentiment_avg']}
            </p>
            <p class="text-sm text-gray-500 mt-1">{sentiment['market_trend']}</p>
        </div>
        
        <div class="metric-card">
            <h3 class="text-sm font-semibold text-gray-500">买入比例</h3>
            <p class="text-3xl font-bold mt-2 text-green-600">{sentiment['buy_ratio']}%</p>
            <p class="text-sm text-gray-500 mt-1">看多信号占比</p>
        </div>
        
        <div class="metric-card">
            <h3 class="text-sm font-semibold text-gray-500">卖出比例</h3>
            <p class="text-3xl font-bold mt-2 text-red-600">{sentiment['sell_ratio']}%</p>
            <p class="text-sm text-gray-500 mt-1">看空信号占比</p>
        </div>
    </div>
    
    <div class="metric-card">
        <h3 class="text-lg font-semibold mb-4">📈 情绪趋势</h3>
        <p class="text-gray-600">数据点数: {sentiment['data_points']}</p>
        <p class="text-gray-600">持有比例: {sentiment['hold_ratio']}%</p>
    </div>
    
    <div class="mt-8 text-center">
        <a href="./index.html" class="text-blue-600 hover:underline">← 返回主页</a>
    </div>
</div>
</body>
</html>"""
    
    output_file = REPORT_REPO / "sentiment.html"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f"已生成: {output_file}")


def deploy_to_github():
    """部署到 GitHub"""
    import subprocess
    
    os.chdir(REPORT_REPO)
    
    # 添加文件
    subprocess.run(['git', 'add', '.'], check=True)
    
    # 提交
    commit_msg = f"Update stock report - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    subprocess.run(['git', 'commit', '-m', commit_msg], check=True)
    
    # 推送
    subprocess.run(['git', 'push', 'origin', 'main'], check=True)
    
    logger.info("✅ 部署完成!")


def main():
    parser = argparse.ArgumentParser(description="GitHub Pages 数据生成器")
    parser.add_argument("--date", type=str, help="交易日期 (YYYYMMDD)")
    parser.add_argument("--deploy", action="store_true", help="生成后自动部署")
    parser.add_argument("--sentiment", action="store_true", help="生成情绪因子页面")
    args = parser.parse_args()
    
    try:
        # 生成数据
        data = generate_pages_data(args.date)
        
        # 保存数据
        save_pages_data(data)
        
        # 生成情绪页面
        if args.sentiment:
            generate_sentiment_page()
        
        # 部署
        if args.deploy:
            deploy_to_github()
        
        logger.info("✅ 完成!")
        return 0
        
    except Exception as e:
        logger.error(f"失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
