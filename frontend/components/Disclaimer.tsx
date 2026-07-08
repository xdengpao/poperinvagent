// 非投资建议声明（R14.9/CP-8）：前端涉标的输出恒附，不可移除。
export const DISCLAIMER =
  '本系统一切意见为框架规则运算结果，仅用于教育与研究，不构成投资建议，不构成收益承诺，' +
  '不保证正确；按框架操作仍可能亏损大部分本金。最终投资决策与执行由用户独立作出并复核签认，盈亏自负。'

export default function Disclaimer() {
  return (
    <footer style={{ marginTop: 40, padding: 16, borderTop: '1px solid #ddd', fontSize: 12, color: '#666' }}>
      {DISCLAIMER}
    </footer>
  )
}
