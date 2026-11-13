from pathlib import Path

base = Path('/Users/fisher/.openclaw/workspace/stock-report')

replacements = {
    'index.html': [
        ('<p class="hero-sub">先看市场，再看策略，再落到个股，最后回到复盘。</p>', '<p class="hero-sub">总览 / 市场 / 策略 / 个股 / 复盘</p>'),
        ('''<div class="hero-answer">\n            <div class="hero-answer-label">本页回答什么</div>\n            <div class="hero-answer-text">这轮结果能不能看，现在该去哪一层。</div>\n          </div>''', ''),
        ('<h2 class="section-title">先判这轮输出能不能用</h2>', '<h2 class="section-title">执行总览</h2>'),
        ('<p class="section-sub">把发布、校验、AI 补齐和市场节奏压成一块判断面板，不再分散看。</p>', '<p class="section-sub">发布 / 校验 / AI / 市场</p>'),
        ('<h2 class="section-title">可执行度看板</h2>', '<h2 class="section-title">核心状态</h2>'),
        ('<p class="section-sub">把四个关键闸门压成一个就绪度面板。</p>', '<p class="section-sub">四项关键指标</p>'),
        ('<h2 class="section-title">先判执行，再看市场、策略和个股</h2>', '<h2 class="section-title">今日主线</h2>'),
        ('<p class="section-sub">首页不再承担跳转菜单功能，只把今天最关键的三层关系压缩成一块主题判断板。</p>', '<p class="section-sub">市场 / 策略 / 个股摘要</p>'),
        ('<h2 class="section-title">今天到底该怎么读这个系统</h2>', '<h2 class="section-title">阅读框架</h2>'),
        ('<p class="section-sub">先看能否执行，再看主线是否支持，最后才看个股层动作，复盘与研究放到后验环节。</p>', '<p class="section-sub">市场 → 策略 → 个股 → 复盘</p>'),
        ('只给出全局结论，不替代市场页、策略页和个股页的明细判断。', '只给全局结论。'),
        ('总控结论 → 市场温度 → 策略支持度 → 候选分层 → 复盘验证。', '市场 → 策略 → 个股 → 复盘'),
        ('<h2 class="section-title">今天先看哪三件事</h2>', '<h2 class="section-title">三步</h2>'),
        ('<p class="section-sub">把建议路径保留为阅读顺序，不再做内容区跳转按钮。</p>', '<p class="section-sub">市场 / 策略 / 执行</p>'),
        ('<h2 class="section-title">这页只负责定调，不负责站内导航</h2>', '<h2 class="section-title">页面职责</h2>'),
        ('<p class="section-sub">内容区不再出现跳转按钮，首页只把“能不能执行、先看什么、今天主线是什么”讲清楚。</p>', '<p class="section-sub">只看结论、顺序、主线</p>'),
        ('确认今天的风险分、主线强度和结构广度，避免直接从个股倒推结论。', '先看风险、主线、广度。'),
        ('只判断启动前夕是否获得主线支持，而不是同时比较多条公开策略。', '只看启动前夕是否成立。'),
        ('候选股只做主攻、观察、回避三层分组，不在首页直接替代个股页判断。', '主攻 / 观察 / 回避。'),
    ],
    'market-overview.html': [
        ('<p class="hero-sub">先把今天的市场环境看清。</p>', '<p class="hero-sub">风险 / 广度 / 主线 / 午盘</p>'),
        ('''<div class="hero-answer">\n            <div class="hero-answer-label">本页回答什么</div>\n            <div class="hero-answer-text">今天能不能做，下一步该不该进策略层。</div>\n          </div>''', ''),
        ('<h2 class="section-title">今天的市场结论先在这里定调</h2>', '<h2 class="section-title">市场总览</h2>'),
        ('<p class="section-sub">把市场状态、主线数量、广度和午盘来源压到同一个总控区里看。</p>', '<p class="section-sub">风险 / 主线 / 广度 / 午盘</p>'),
        ('<h2 class="section-title">先看市场有没有扩散</h2>', '<h2 class="section-title">广度</h2>'),
        ('<p class="section-sub">上涨占比和平均涨跌直接放在一个宽度面板里。</p>', '<p class="section-sub">上涨占比 / 平均涨跌</p>'),
        ('<h2 class="section-title">开盘前先把风险口径定下来</h2>', '<h2 class="section-title">晨判</h2>'),
        ('<p class="section-sub">晨判不再是一段报告，而是起手式、风险和观察方向的工作区。</p>', '<p class="section-sub">风险 / 剧本 / 焦点</p>'),
        ('<h2 class="section-title">先看剧本，再盯焦点</h2>', '<h2 class="section-title">开盘指挥板</h2>'),
        ('<p class="section-sub">把开盘动作和焦点行业压成两列短清单。</p>', '<p class="section-sub">剧本 / 行业 / 驱动</p>'),
        ('<h2 class="section-title">盘中只验证两件事：主线有没有兑现、节奏有没有变</h2>', '<h2 class="section-title">午盘</h2>'),
        ('<p class="section-sub">把午盘总结、操作建议和盘中快照压成一个兑现面板。</p>', '<p class="section-sub">研判 / 建议 / 快照</p>'),
        ('<h2 class="section-title">把热点、推荐和观察池并排盯</h2>', '<h2 class="section-title">盘中热点板</h2>'),
        ('<p class="section-sub">盘中不读长文，直接看三块短板。</p>', '<p class="section-sub">热点 / 推荐 / 观察池</p>'),
        ('<h2 class="section-title">这页只负责给出市场环境判断</h2>', '<h2 class="section-title">页面职责</h2>'),
        ('<p class="section-sub">市场页不再承担站内跳转职责，只负责把晨判、午盘和结构变化压成一个环境判断屏。</p>', '<p class="section-sub">环境判断页</p>'),
        ('先看风险分、主线行业和午盘口径，再判断策略层是否值得继续看。', '风险分 / 主线 / 午盘'),
    ],
    'strategy-vs-market.html': [
        ('<p class="hero-sub">这一层只判断启动前夕今天该不该上。</p>', '<p class="hero-sub">激活 / 命中 / 节奏</p>'),
        ('''<div class="hero-answer">\n            <div class="hero-answer-label">本页回答什么</div>\n            <div class="hero-answer-text">启动前夕今天该不该主导执行。</div>\n          </div>''', ''),
        ('<h2 class="section-title">先判断策略是否被市场支持</h2>', '<h2 class="section-title">策略判定</h2>'),
        ('<p class="section-sub">这里不是展示策略，而是判断启动前夕今天能不能做。</p>', '<p class="section-sub">激活 / 命中 / 节奏</p>'),
        ('<h2 class="section-title">当前公开策略卡</h2>', '<h2 class="section-title">策略卡</h2>'),
        ('<p class="section-sub">用一张卡看清：能不能上、为什么上、上去看什么。</p>', '<p class="section-sub">状态 / 支持 / 样本</p>'),
        ('<h2 class="section-title">市场 × 策略 行业动作表</h2>', '<h2 class="section-title">行业动作表</h2>'),
        ('<p class="section-sub">这里是市场和策略的交汇处。</p>', '<p class="section-sub">市场与策略交集</p>'),
        ('<h2 class="section-title">策略页只负责判断这条策略值不值得信</h2>', '<h2 class="section-title">页面职责</h2>'),
        ('<p class="section-sub">不再在内容区放跳转按钮，策略页的职责是把市场支持度、行业动作和候选承接关系讲清楚。</p>', '<p class="section-sub">只看策略是否成立</p>'),
        ('return `当前市场更偏向 ${top}。策略层的职责不是自己定义方向，而是判断“启动前夕”是否真正命中这些市场主线。`;', 'return `主线：${top}。只看启动前夕是否命中。`;'),
        ('return `如果策略是激活态，就去候选股中心看那 20 只票里的主攻/观察/回避；如果只是观察态，就不要把执行层当成确定性信号。`;', 'return `激活看执行层；观察态不直接下结论。`;'),
        ('if (row.activation === \'active\') return \'市场已经给了这条策略足够支持，今天可以把它当成主执行策略来用。\';', 'if (row.activation === \'active\') return \'可作为主执行策略。\';'),
        ('if (row.activation === \'watch\') return \'这条策略目前处于观察态，说明市场还没有完全确认，不宜过度前冲。\';', 'if (row.activation === \'watch\') return \'观察态，暂不前冲。\';'),
        ('return \'当前不建议把它作为主攻策略，只保留跟踪价值。\';', 'return \'暂不作为主攻。\';'),
    ],
    'decision-candidates.html': [
        ('<p class="hero-sub">市场和策略确认后，再来这里看具体票。</p>', '<p class="hero-sub">主攻 / 观察 / 回避</p>'),
        ('''<div class="hero-answer">\n            <div class="hero-answer-label">本页回答什么</div>\n            <div class="hero-answer-text">哪些主攻，哪些观察，哪些回避。</div>\n          </div>''', ''),
        ('<div class="hero-preview-sub">先看摘要，再决定是否展开详情。</div>', '<div class="hero-preview-sub">三层分组摘要</div>'),
        ('<h2 class="section-title">先筛选，再判断</h2>', '<h2 class="section-title">筛选器</h2>'),
        ('<p class="section-sub">先筛，再看。</p>', '<p class="section-sub">角色 / 策略 / 行业</p>'),
        ('<h2 class="section-title">当前执行清单</h2>', '<h2 class="section-title">执行清单</h2>'),
        ('<p class="section-sub">每张卡只保留执行所需信息。</p>', '<p class="section-sub">只保留执行信息</p>'),
        ('<h2 class="section-title">这页只负责执行分层，不再承担站内跳转</h2>', '<h2 class="section-title">页面职责</h2>'),
        ('<p class="section-sub">先看主攻、观察、回避三层结果，再决定是否展开股票细节，不在内容区继续放导航按钮。</p>', '<p class="section-sub">只看三层分组</p>'),
        ('集中盯最值得跟踪的少数标的，避免在 20 只票之间平均分散注意力。', '优先跟踪。'),
        ('保留条件确认空间，不把信息不充分的标的误判成主攻。', '等待条件确认。'),
        ('明确剔除弱票，给执行层留出更清晰的注意力带宽。', '直接剔除。'),
    ],
    'research-lab.html': [
        ('<div class="hero-sub">主流程是否可信，研究结论是否还新鲜。</div>', '<div class="hero-sub">链路 / 新鲜度 / 复用性</div>'),
        ('<div class="hero-focus-copy">这页先回答“这轮结果是否值得继续研究”，不再在内容区放站内跳转按钮。</div>', '<div class="hero-focus-copy">先看链路与新鲜度。</div>'),
        ('先确认链路可信度与研究是否新鲜，再决定是否继续深入。', '先看链路与新鲜度。'),
        ('短线看临界因子，长线看基础就绪，再用复盘样本验证研究是否还能复用。', '短线 / 长线 / 复盘验证'),
        ('<div class="muted">只看还能复用的部分</div>', '<div class="muted">当前可复用项</div>'),
        ('<div class="muted">先验收，再决定下一轮。</div>', '<div class="muted">当前结论</div>'),
    ],
    'recommendation-review.html': [
        ('<div class="hero-sub">先看总体表现，再向下钻明细。</div>', '<div class="hero-sub">成绩单 / 明细</div>'),
        ('<div class="hero-focus-copy">这页先看总体成绩单，再下钻明细表，不再在内容区放回跳型按钮。</div>', '<div class="hero-focus-copy">先看成绩单，再看明细。</div>'),
        ('先用成绩单看整体表现，再决定是否有必要继续翻表格和样本细节。', '先看成绩单。'),
        ('优先看最高频推荐、次日均值和累计均值，避免一开始就掉进长表格里。', '高频推荐 / 次日均值 / 累计均值'),
        ('<div class="muted">复盘关注度最高的股票</div>', '<div class="muted">高频推荐对象</div>'),
        ('<div class="muted">先缩小范围，再看明细。</div>', '<div class="muted">先筛选，再看明细</div>'),
        ('<div class="muted">看完细节，再回研究层总结。</div>', '<div class="muted">复盘提示</div>'),
    ],
}

for file, pairs in replacements.items():
    p = base / file
    text = p.read_text(encoding='utf-8')
    for old, new in pairs:
        if old in text:
            text = text.replace(old, new, 1)
    p.write_text(text, encoding='utf-8')

print('copy trim applied')
