// The key source of truth for every user-facing string in the app.
//
// `zh-CN.ts` is typed against this object (see its `Resources` annotation), so
// a key added here without a Chinese translation is a compile error rather
// than a string that silently renders in the wrong language at runtime.
//
// British spelling throughout ("organisation", "behaviour", "recognise") --
// the project convention, and one of the inconsistencies this file exists to
// stop drifting again.
export const en = {
  nav: {
    brand: 'bestteam',
    dashboard: 'Dashboard',
    buildTeam: 'Build a team',
    myTeams: 'My teams',
    runTeam: 'Run a team',
    accounts: 'Accounts',
    advanced: 'Advanced',
    memory: 'Memory',
    trace: 'Trace',
    logOut: 'Log out',
    // The language switcher labels each option in its OWN language, never
    // translated -- someone who has landed in a language they cannot read
    // needs to recognise their own by sight to get out.
    language: 'Language',
  },
  common: {
    tryAgain: 'Try again',
    loading: 'Loading…',
    copy: 'Copy',
    copied: 'Copied!',
    copyFailed: "Couldn't copy",
  },
  runStatus: {
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    any: 'Any status',
  },
  traceEvents: {
    queued: 'Waiting to start',
    started: 'Your team got started',
    agentDone: '{{agent}} finished their part',
    completed: 'All done!',
    failed: 'Something went wrong',
    cancelled: 'Stopped',
  },
  run: {
    title: 'Run a team',
    subtitle: 'Choose a team, give it a task, and follow its progress.',
    // Deliberately says nothing about which host is unreachable or what is
    // meant to be running there: this page is the customer's daily driver,
    // and the deployment's internals are not theirs to debug. The technical
    // detail goes to console.error for whoever is.
    unreachable: "We can't reach the service right now. This is usually temporary.",
    teamLabel: 'Team',
    noTeams: 'No teams yet — build one first.',
    taskLabel: 'What should this team do?',
    taskPlaceholder: 'Describe what you would like this team to do...',
    start: 'Run',
    running: 'Running…',
    stop: 'Stop',
    stopping: 'Stopping…',
    runningFor: 'Running for {{seconds}}s',
    connected: 'Connected',
    connecting: 'Connecting…',
    disconnected: 'Disconnected',
    waitingFirstStep: 'Waiting for your team to start work…',
    stale: 'No update for {{seconds}}s — still working, this can take a while for longer tasks.',
    runAgain: 'Run again',
    progress: 'Progress',
    showTechnical: 'Show technical trace',
    hideTechnical: 'Hide technical trace',
    noRunYet: 'No run yet — pick a team and hit Run.',
  },
  activity: {
    title: 'Team activity',
    subtitle: "See automations at a glance, or dig into any run's history.",
    tabAutomations: 'Automations',
    tabRuns: 'Runs',
    tabShared: 'Shared',
    tabAlerts: 'Alerts',
    tabData: 'Data',
    teamLabel: 'Team',
    allTeams: 'All teams',
    pickTeam: 'Pick a team…',
    triggerLabel: 'Trigger',
    triggerAny: 'Manual + automatic',
    triggerManual: 'Manual only',
    triggerAutomatic: 'Automatic only',
    statusLabel: 'Status',
    noRuns: 'No runs match these filters.',
    manual: 'Manual',
    automatic: 'Automatic',
    close: 'Close',
    // The panel heading names the team and when it ran. The run's id is still
    // shown, but as a copyable detail rather than as the title -- a bare UUID
    // told a customer nothing and read like a page they weren't meant to see
    // (audit finding F7).
    runIdLabel: 'Run ID',
  },
  confirm: {
    // A non-technical customer has no basis to choose between model specs, so
    // the picker defaults and hides. It stays reachable, because someone who
    // does know what they want must not be locked out (audit finding F9).
    advancedToggleShow: 'Advanced settings',
    advancedToggleHide: 'Hide advanced settings',
    modelLabel: 'Which assistant should your team use?',
    reqModelLabel: 'Which assistant should redo this?',
  },
  modelCatalog: {
    // Shared by every wizard stage that needs a real model to generate with.
    loadFailed: "Couldn't load the available AI models. Check your connection and try again.",
    empty: 'No AI models are available yet. Contact your administrator, or try again.',
  },
}

// Note: deliberately NOT `as const`. Under `as const` every value becomes its
// own literal type, so `zhCN: Resources` would demand the *English string* at
// each key -- exactly backwards. Plain inference widens the values to `string`
// while keeping the key structure required, which is the property we want: a
// missing or misspelled key is a compile error, a different translation is not.
export type Resources = typeof en
