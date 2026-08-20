import type { Resources } from './en'

// Typed against `en`, so every key must be present and correctly shaped.
// A missing translation fails `npm run build`, not a customer's screen.
//
// `DeepPartial` is deliberately NOT used here: a half-translated app that
// silently falls back to English mid-sentence is worse than a build error.
export const zhCN: Resources = {
  nav: {
    brand: 'bestteam',
    dashboard: '概览',
    buildTeam: '组建团队',
    myTeams: '我的团队',
    runTeam: '运行团队',
    accounts: '账户管理',
    advanced: '高级配置',
    memory: '记忆库',
    trace: '运行追踪',
    logOut: '退出登录',
    language: '语言',
  },
}
