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
  },
  run: {
    // Deliberately says nothing about which host is unreachable or what is
    // meant to be running there: this page is the customer's daily driver,
    // and the deployment's internals are not theirs to debug. The technical
    // detail goes to console.error for whoever is.
    unreachable: "We can't reach the service right now. This is usually temporary.",
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
