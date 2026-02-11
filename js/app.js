// 股票数据
const stockData = [
    { rank: 1, code: '000338', score: 67.0, price: 25.97, change: 49.3, support: 26.61, resistance: 29.81, position: '20%', risk: 'low' },
    { rank: 2, code: '000630', score: 60.1, price: 7.18, change: 23.4, support: 7.54, resistance: 7.68, position: '15%', risk: 'medium' },
    { rank: 3, code: '000157', score: 59.1, price: 9.75, change: 12.2, support: 9.86, resistance: 10.11, position: '15%', risk: 'medium' },
    { rank: 4, code: '000100', score: 58.1, price: 4.72, change: 3.7, support: 4.83, resistance: 4.77, position: '15%', risk: 'medium' },
    { rank: 5, code: '000568', score: 58.0, price: 117.94, change: -0.1, support: 119.52, resistance: 117.96, position: '15%', risk: 'medium' },
    { rank: 6, code: '000725', score: 57.7, price: 4.29, change: 1.4, support: 4.36, resistance: 4.31, position: '15%', risk: 'medium' },
    { rank: 7, code: '000425', score: 56.9, price: 12.10, change: 4.9, support: 12.28, resistance: 12.28, position: '15%', risk: 'medium' },
    { rank: 8, code: '000333', score: 55.9, price: 79.69, change: 1.3, support: 80.92, resistance: 80.00, position: '15%', risk: 'medium' },
    { rank: 9, code: '000063', score: 55.7, price: 37.35, change: -2.4, support: 38.30, resistance: 37.61, position: '15%', risk: 'medium' },
    { rank: 10, code: '000301', score: 55.2, price: 13.20, change: 23.4, support: 13.72, resistance: 14.13, position: '15%', risk: 'medium' },
    { rank: 11, code: '000001', score: 52.7, price: 11.07, change: -3.7, support: 11.14, resistance: 11.19, position: '10%', risk: 'high' },
    { rank: 12, code: '000002', score: 54.5, price: 4.89, change: 2.9, support: 5.01, resistance: 4.93, position: '10%', risk: 'high' },
    { rank: 13, code: '000596', score: 54.4, price: 132.01, change: -2.4, support: 135.09, resistance: 132.97, position: '10%', risk: 'high' },
    { rank: 14, code: '000408', score: 53.4, price: 84.84, change: -5.7, support: 87.17, resistance: 86.30, position: '10%', risk: 'high' },
    { rank: 15, code: '000166', score: 52.6, price: 5.09, change: -3.8, support: 5.14, resistance: 5.15, position: '10%', risk: 'high' },
    { rank: 16, code: '000651', score: 52.3, price: 38.72, change: -5.0, support: 39.39, resistance: 39.30, position: '10%', risk: 'high' },
    { rank: 17, code: '000538', score: 52.2, price: 57.21, change: -0.2, support: 57.74, resistance: 57.24, position: '10%', risk: 'high' },
    { rank: 18, code: '000617', score: 51.7, price: 8.89, change: -7.0, support: 9.07, resistance: 9.08, position: '10%', risk: 'high' },
    { rank: 19, code: '000625', score: 51.1, price: 11.18, change: -5.5, support: 11.31, resistance: 11.36, position: '10%', risk: 'high' },
    { rank: 20, code: '000661', score: 50.8, price: 88.09, change: -7.1, support: 90.79, resistance: 89.96, position: '10%', risk: 'high' }
];

// 因子详情数据
const factorDetails = {
    '000338': { momentum: 83.7, liquidity: 60.4, volatility: 47.9, strength: 58.9, riskReward: 9.99 },
    '000630': { momentum: 64.5, liquidity: 66.1, volatility: 27.1, strength: 65.2, riskReward: 2.33 },
    '000157': { momentum: 56.2, liquidity: 45.7, volatility: 58.7, strength: 58.8, riskReward: 5.37 },
    '000100': { momentum: 50.0, liquidity: 60.9, volatility: 49.8, strength: 59.3, riskReward: 0.84 },
    '000568': { momentum: 47.2, liquidity: 52.8, volatility: 57.0, strength: 65.0, riskReward: -0.02 },
    '000725': { momentum: 48.2, liquidity: 63.2, volatility: 55.3, strength: 52.4, riskReward: 0.46 },
    '000425': { momentum: 50.8, liquidity: 49.3, volatility: 55.7, strength: 55.3, riskReward: 1.62 },
    '000333': { momentum: 48.1, liquidity: 61.2, volatility: 55.4, strength: 42.0, riskReward: 0.42 },
    '000063': { momentum: 45.5, liquidity: 65.9, volatility: 47.2, strength: 49.3, riskReward: -0.46 },
    '000301': { momentum: 64.5, liquidity: 36.8, volatility: 35.9, strength: 52.6, riskReward: 2.97 }
};

// 初始化页面
document.addEventListener('DOMContentLoaded', function() {
    renderStockTable();
    renderDetailCards();
    initCharts();
});

// 渲染股票表格
function renderStockTable() {
    const tbody = document.getElementById('stockTable');
    tbody.innerHTML = '';

    stockData.forEach(stock => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td class="rank">${stock.rank}</td>
            <td><strong>${stock.code}</strong></td>
            <td class="score">${stock.score.toFixed(1)}</td>
            <td>${stock.price.toFixed(2)}</td>
            <td class="${stock.change >= 0 ? 'positive' : 'negative'}">${stock.change >= 0 ? '+' : ''}${stock.change.toFixed(1)}%</td>
            <td>${stock.support.toFixed(2)}</td>
            <td>${stock.resistance.toFixed(2)}</td>
            <td><span class="position-tag ${getPositionClass(stock.position)}">${stock.position}</span></td>
        `;
        tbody.appendChild(row);
    });
}

// 获取仓位标签样式
function getPositionClass(position) {
    if (position === '20%') return 'position-high';
    if (position === '15%') return 'position-medium';
    return 'position-low';
}

// 渲染详细分析卡片
function renderDetailCards() {
    const grid = document.getElementById('detailGrid');
    grid.innerHTML = '';

    stockData.slice(0, 10).forEach((stock, index) => {
        const factors = factorDetails[stock.code] || {
            momentum: stock.score * 0.8,
            liquidity: stock.score * 0.7,
            volatility: stock.score * 0.8,
            strength: stock.score * 0.75,
            riskReward: 1.0
        };

        const riskLabel = stock.risk === 'low' ? '🟢 低风险' : 
                         stock.risk === 'medium' ? '🟡 中风险' : '🔴 高风险';
        
        const expectedReturn = ((stock.resistance - stock.price) / stock.price * 100).toFixed(1);
        const riskLoss = ((stock.price - stock.support) / stock.price * 100).toFixed(1);

        const card = document.createElement('div');
        card.className = 'detail-card';
        card.style.animationDelay = `${index * 0.1}s`;
        card.innerHTML = `
            <div class="detail-header">
                <h3>🏆 ${stock.rank}. ${stock.code}</h3>
                <span class="detail-score">${stock.score.toFixed(1)}/100</span>
            </div>
            <div class="detail-factors">
                <div class="factor-badge">
                    <span class="label">动量</span>
                    <span class="value">${factors.momentum.toFixed(1)}</span>
                </div>
                <div class="factor-badge">
                    <span class="label">流动</span>
                    <span class="value">${factors.liquidity.toFixed(1)}</span>
                </div>
                <div class="factor-badge">
                    <span class="label">波动</span>
                    <span class="value">${factors.volatility.toFixed(1)}</span>
                </div>
                <div class="factor-badge">
                    <span class="label">强度</span>
                    <span class="value">${factors.strength.toFixed(1)}</span>
                </div>
                <div class="factor-badge">
                    <span class="label">风险收益</span>
                    <span class="value">${factors.riskReward.toFixed(2)}</span>
                </div>
            </div>
            <div class="detail-advice">
                <div class="advice-row">
                    <span class="label">当前价格</span>
                    <span class="value">${stock.price.toFixed(2)}元</span>
                </div>
                <div class="advice-row">
                    <span class="label">止损位</span>
                    <span class="value red">${stock.support.toFixed(2)}元</span>
                </div>
                <div class="advice-row">
                    <span class="label">目标位</span>
                    <span class="value green">${stock.resistance.toFixed(2)}元 (+${expectedReturn}%)</span>
                </div>
                <div class="advice-row">
                    <span class="label">建议仓位</span>
                    <span class="value">${stock.position}</span>
                </div>
                <div class="advice-row">
                    <span class="label">风险等级</span>
                    <span class="value">${riskLabel}</span>
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

// 初始化图表
function initCharts() {
    // 检查Chart.js是否加载
    if (typeof Chart === 'undefined') {
        console.log('Chart.js not loaded, skipping chart initialization');
        return;
    }

    // 得分分布图
    const ctx1 = document.getElementById('scoreChart');
    if (ctx1) {
        new Chart(ctx1, {
            type: 'bar',
            data: {
                labels: stockData.map(s => s.code),
                datasets: [{
                    label: '综合得分',
                    data: stockData.map(s => s.score),
                    backgroundColor: stockData.map(s => {
                        if (s.score >= 60) return 'rgba(76, 175, 80, 0.8)';
                        if (s.score >= 55) return 'rgba(255, 193, 7, 0.8)';
                        return 'rgba(244, 67, 54, 0.8)';
                    }),
                    borderColor: stockData.map(s => {
                        if (s.score >= 60) return '#4caf50';
                        if (s.score >= 55) return '#ffc107';
                        return '#f44336';
                    }),
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        max: 100,
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        ticks: {
                            color: '#888'
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: '#888'
                        }
                    }
                }
            }
        });
    }

    // 涨跌幅饼图
    const ctx2 = document.getElementById('pieChart');
    if (ctx2) {
        const positive = stockData.filter(s => s.change > 0).length;
        const negative = stockData.filter(s => s.change < 0).length;
        const neutral = stockData.filter(s => s.change === 0).length;

        new Chart(ctx2, {
            type: 'doughnut',
            data: {
                labels: ['上涨', '下跌', '持平'],
                datasets: [{
                    data: [positive, negative, neutral],
                    backgroundColor: [
                        'rgba(76, 175, 80, 0.8)',
                        'rgba(244, 67, 54, 0.8)',
                        'rgba(158, 158, 158, 0.8)'
                    ],
                    borderColor: '#1a1a2e',
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: '#888'
                        }
                    }
                }
            }
        });
    }
}

// 添加样式
const style = document.createElement('style');
style.textContent = `
    .position-tag {
        display: inline-block;
        padding: 5px 12px;
        border-radius: 15px;
        font-weight: bold;
        font-size: 0.85em;
    }
    
    .position-high {
        background: linear-gradient(135deg, rgba(76, 175, 80, 0.3), rgba(76, 175, 80, 0.1));
        border: 1px solid #4caf50;
        color: #4caf50;
    }
    
    .position-medium {
        background: linear-gradient(135deg, rgba(255, 193, 7, 0.3), rgba(255, 193, 7, 0.1));
        border: 1px solid #ffc107;
        color: #ffc107;
    }
    
    .position-low {
        background: linear-gradient(135deg, rgba(244, 67, 54, 0.3), rgba(244, 67, 54, 0.1));
        border: 1px solid #f44336;
        color: #f44336;
    }

    .chart-container {
        height: 300px;
        margin-top: 20px;
    }
`;
document.head.appendChild(style);
