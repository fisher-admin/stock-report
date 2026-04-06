# Index Page Overrides

> **PROJECT:** Stock Report
> **Generated:** 2026-04-06 19:25:54
> **Page Type:** Dashboard / Data View

> ⚠️ **IMPORTANT:** Rules in this file **override** the Master file (`design-system/MASTER.md`).
> Only deviations from the Master are documented here. For all other rules, refer to the Master.

---

## Page-Specific Rules

### Layout Overrides

- **Max Width:** 800px (narrow, focused)
- **Layout:** Single column, centered
- **Sections:** 1. Hero (Video/Mission), 2. Solutions by Industry, 3. Solutions by Role, 4. Client Logos, 5. Contact Sales

### Spacing Overrides

- **Content Density:** Low — focus on clarity

### Typography Overrides

- No overrides — use Master typography

### Color Overrides

- **Strategy:** Corporate: Navy/Grey. High integrity. Conservative accents.

### Component Overrides

- Avoid: Leave UI frozen with no feedback
- Avoid: Expect z-index to work across contexts
- Avoid: Use arbitrary large z-index values

---

## Page-Specific Components

- No unique components for this page

---

## Recommendations

- Effects: KPI value animations (count-up), trend arrow direction animations, metric card hover lift, alert pulse effect
- Animation: Use skeleton screens or spinners
- Layout: Understand what creates new stacking context
- Layout: Define z-index scale system (10 20 30 50)
- CTA Placement: Contact Sales (Primary) + Login (Secondary)
