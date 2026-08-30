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
    // The product slogan, one key for the whole app. It used to be three
    // near-copies -- README's, the wizard heading's and the login page's --
    // that had already drifted apart in wording; the Chinese side had one
    // string all along. README.md carries the fourth copy and cannot read
    // this, so it is the one place to keep in step by hand.
    tagline: 'Intent in, BestTeam out',
    dashboard: 'Dashboard',
    buildTeam: 'Build a team',
    myTeams: 'My teams',
    runTeam: 'Run a team',
    accounts: 'Accounts',
    advanced: 'Advanced',
    memory: 'Memory',
    trace: 'Trace',
    feedback: 'Feedback',
    changePassword: 'Change password',
    logOut: 'Log out',
    // The language switcher labels each option in its OWN language, never
    // translated -- someone who has landed in a language they cannot read
    // needs to recognise their own by sight to get out.
    language: 'Language',
  },
  common: {
    tryAgain: 'Try again',
    cancel: 'Cancel',
    delete: 'Delete',
    remove: 'Remove',
    addItem: '+ add',
    confirmTitle: 'Are you sure?',
    loading: 'Loading…',
    copy: 'Copy',
    copied: 'Copied!',
    copyFailed: "Couldn't copy",
    continue: 'Continue',
    working: 'Working…',
    startOver: 'Start over',
    // Stands in for an image in a team's reply, which is never fetched --
    // see components/MarkdownText.tsx.
    image: 'image',
  },
  // The first screen anyone sees, and the only one outside `Layout` -- so it
  // carries its own language control. Before this namespace existed the page
  // was hardcoded English, which made a Chinese customer's very first
  // impression untranslatable however bilingual the rest of the app was.
  login: {
    heading: 'Log in',
    username: 'Username',
    password: 'Password',
    submit: 'Log in',
    submitting: 'Logging in…',
    showPassword: 'Show password',
    hidePassword: 'Hide password',
    capsLock: 'Caps Lock is on.',
    // Three shipped capabilities, deliberately not three adjectives.
    points: {
      noCode: 'No orchestration code to write',
      seeEverything: 'Watch every step as it happens',
      share: 'Share a link with a colleague',
    },
  },
  password: {
    title: 'Change password',
    current: 'Current password',
    new: 'New password',
    confirm: 'Confirm new password',
    hint: 'At least 8 characters.',
    mismatch: 'The two new passwords do not match.',
    submit: 'Change password',
    submitting: 'Changing…',
    done: 'Done',
    success: 'Your password has been changed. Any other device has been signed out.',
  },
  feedback: {
    title: 'Send feedback',
    kindDefect: 'Report a problem',
    kindSuggestion: 'Make a suggestion',
    bodyLabel: 'Your feedback',
    placeholder: 'Tell us what went wrong, or what you would like to see…',
    submit: 'Send',
    sending: 'Sending…',
    thanks: 'Thank you — your feedback has been recorded.',
    tooMany: 'Feedback limit reached for today — please try again tomorrow.',
    failed: "Couldn't send your feedback. Please try again.",
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
    // Only used when two teams share a friendly name, so the option a
    // customer picks is never ambiguous. Nothing constrains `display_name`
    // to be unique, and two identical options pointing at different teams
    // is worse than showing one technical name.
    teamLabelAmbiguous: '{{display}} ({{name}})',
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
    tabOverview: 'Overview',
    tabAutomations: 'Automations',
    tabRuns: 'Runs',
    tabAlerts: 'Alerts',
    tabData: 'Data',
    teamLabel: 'Team',
    allTeams: 'All teams',
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
  // The Activity page's default landing tab: how much work the customer's
  // teams have done for them. Deliberately no model name or token/cost figure
  // anywhere here -- that stays admin-only (see EmailBudgetSettings and the
  // MonitorPage "Not Found" fix for the same boundary).
  overview: {
    title: 'Your overview',
    subtitle: 'How much your teams have been working for you.',
    empty: 'No runs yet — once your teams get to work, your stats will show up here.',
    completedLabel: 'Tasks completed',
    teamTaskCount: '{{count}} tasks',
    // A team can be deleted while the work it completed stays on the record.
    deletedTeam: '(deleted)',
    activeDays: 'Active days',
    currentStreak: 'Current streak',
    longestStreakNote: 'Personal best: {{count}} days',
    heatmapCaption: 'How often your team has been busy',
    loadFailed: "Couldn't load your activity. Check your connection and try again.",
  },
  advanced: {
    filterPlaceholder: 'Filter…',
    noMatches: 'Nothing matches that filter.',
    // The JSON editor's only feedback was "Not valid JSON", which does not say
    // where to look in an 18-row document (audit finding F15).
    invalidJson: 'Not valid JSON — {{detail}}',
  },
  // The five-stage Build-a-team wizard. Its stage pages kept English literals
  // long after the rest of the app was extracted, so a Chinese customer met a
  // Chinese nav sitting above an entirely English wizard -- the one surface
  // where that matters most, since it is the product's front door.
  //
  // Where a literal duplicated a string this file already had (the trace-event
  // titles PreviewPage re-implemented, the model-catalog errors IntentPage and
  // DocumentsPage each hardcoded), the page now reads the existing key rather
  // than getting a second copy here.
  wizard: {
    subtitle: "Answer a few questions and we'll design, test, and launch a custom AI team for you.",
    sessionLoadFailed: "Couldn't load this session: {{detail}}",
    optional: '(optional)',
    needMore: 'We need a bit more information first.',
    teamMode: {
      sequential: 'Step by step',
      parallel: 'All at once',
      hierarchical: 'Led by a manager',
    },
    confirm: {
      title: 'Confirm your team',
      adjustTitle: 'Anything to adjust?',
      adjustSubtitle:
        "Here's your team again. If something's off, describe the change and we'll redesign it — you can always go back to try it out again.",
      historyHeading: 'Adjustments so far:',
      changeLabel: 'What should change?',
      changePlaceholder:
        'e.g. We also use Zendesk for tickets, and the team should check our FAQ document before replying.',
      uploadLink: 'Need to add or update a document? Upload it here',
      apply: 'Update the team',
      updating: 'Updating…',
      updatingNotice:
        'Redesigning your team — this usually takes under a minute. Please stay on this page, and hold off on the other buttons until it finishes.',
      // Two whole sentences, not a 'Show'/'Hide' word swapped into one: a
      // translated fragment assembled at runtime only reads correctly in the
      // language whose grammar the split was chosen for.
      showUnderstanding: 'Show what we understood about your business',
      teamHeading: 'Your team',
      hideUnderstanding: 'Hide what we understood about your business',
      noSummary: 'No summary was generated for this session.',
      generate: 'Generate summary',
      generating: 'Generating…',
      clarifyHeading: 'A couple of quick questions:',
      clarifyHint: 'Your answers are applied when you press "Update the team".',
      summaryLabel: 'Summary',
      painPoints: 'Pain points',
      painPointsPlaceholder: 'e.g. replies take too long',
      goals: 'Goals',
      goalsPlaceholder: 'e.g. reply within an hour',
      successCriteria: 'What does success look like?',
      successPlaceholder: 'e.g. fewer escalations',
      constraints: 'Constraints',
      constraintsPlaceholder: 'e.g. must stay in English',
      backToPreview: 'Back to preview',
      continueToDeploy: 'Continue to deploy',
    },
    steps: {
      intent: 'Your challenge',
      questions: 'A few questions',
      documents: 'Your documents',
      preview: 'Meet your team',
      confirm: 'Confirm',
      deploy: 'Go live',
    },
    questions: {
      title: 'A few quick questions',
      subtitle:
        "Your answers help us design the right team. You can skip any question — or all of them — and we'll make a sensible assumption you can review in the summary.",
      answerPlaceholder: 'Type your answer (optional)',
      skip: 'Skip these questions',
      updating: 'Updating what we understood…',
      updatingNotice: "We're folding your answers into what we understood about your business — this takes a moment.",
      noQuestions: 'No open questions — your description gave us what we need.',
    },
    intent: {
      title: 'Tell us about your challenge',
      subtitle:
        "Describe what you're hoping an AI team could take off your plate. No technical detail needed — plain language is perfect.",
      uploadRecording: 'Upload interview recording',
      replaceRecording: 'Replace recording',
      transcribing: 'Transcribing interview…',
      extracting: 'Extracting key points…',
      seeTranscript: 'See full transcript',
      orDescribe: 'or describe it below',
      intentLabel: 'What do you want help with?',
      intentPlaceholder:
        "e.g. We get dozens of customer support emails a day and can't keep up with replies.",
      asIsLabel: 'How do you handle this today?',
      asIsPlaceholder:
        'e.g. One person reads every email and replies manually using a few canned templates.',
      start: 'Start building my team',
      starting: 'Starting…',
      creating: 'Setting things up…',
      requirements: 'Getting to know your business…',
      loadingModels: 'Loading available models…',
    },
    documents: {
      title: 'Add your documents',
      subtitle:
        'If your AI team should be able to answer questions from your own files — policies, FAQs, manuals — upload them here. Optional: you can always skip this and add documents later.',
      nameLabel: 'What should we call these documents?',
      nameHint: "(required if you're uploading)",
      namePlaceholder: 'e.g. Product policies',
      nameRequired: 'Give your documents a short name first (e.g. "Product policies").',
      descriptionLabel: "What's in these documents? (one sentence)",
      descriptionPlaceholder: 'e.g. Refund, delivery and warranty policies for our online shop',
      descriptionHint: 'This helps your AI team know when to look here for an answer.',
      searchQuality: 'Search quality',
      standard: 'Standard',
      enhanced: 'Enhanced',
      searchQualityHint:
        'Enhanced finds more relevant answers in your documents. Takes a little longer to index.',
      chooseFiles: 'Choose files…',
      removeFile: 'Remove {{name}}',
      skip: 'Skip for now',
      uploading: 'Uploading your documents…',
      ingesting: 'Processing your documents…',
      generating: 'Putting your team together…',
      // The re-upload dialog. `detail` is the backend's own 409 message,
      // describing the collection as it stands today; the sentence after it
      // says what this upload would make of it, so the one dialog the customer
      // has to answer carries both halves of the change.
      existsTitle: 'This collection already exists',
      existsBody: '{{detail}} Either way it will be re-indexed with {{quality}} search.',
      existsReplace: 'Replace everything',
      existsAdd: 'Add to it',
      pickCollectionHint: 'Your team already searches more than one collection. Which one are you updating?',
      existingFilesTitle: 'Files already in "{{name}}"',
      removeExistingFile: 'Remove {{name}}',
      removeExistingConfirmTitle: 'Remove "{{name}}"?',
      removeExistingConfirmBody: 'It will be taken out of "{{kb}}" and your team can no longer search it.',
      removeExistingConfirmBodyShared: 'It will be taken out of "{{kb}}", and these teams can no longer search it: {{teams}}.',
      removeExistingBlockedProcessing: 'These documents are still being processed. Wait for that to finish, then remove one.',
      removeExistingBlockedOnly: 'This is the only document we can read here, and a collection can’t be empty. Delete the whole collection instead.',
      reviewTitle: 'Here’s what "{{name}}" contains now',
      stillProcessing:
        'Your documents are still being processed — this is taking longer than expected. They’re safely uploaded; come back in a moment and continue from here.',
      processingFailedDetail: 'Processing failed: {{detail}}',
      processingFailed: 'Processing your documents failed.',
    },
    preview: {
      title: 'Meet your team',
      subtitle: 'Here’s the team we’ve put together for "{{name}}". Try giving them a real task below.',
      mailboxHint:
        'Connect your mailbox to try the team against your real inbox below — or skip for now and connect before you go live.',
      tryThemOut: 'Try them out',
      taskLabel: 'A real task or message for your team',
      taskPlaceholder:
        'e.g. A customer is asking how to reset their password and is getting frustrated.',
      run: 'Run this through your team',
      lostConnection:
        'Lost connection to the backend while your team was working. Please try again.',
    },
    deploy: {
      title: 'Go live',
      designFirst: 'Design your team first, then come back here to launch it.',
      readyTitle: 'Ready to go live?',
      readySubtitle:
        'Once you launch, "{{name}}" will be available to handle real requests. You can always come back and adjust it later.',
      launch: 'Launch my team',
      launching: 'Launching…',
      connectMailboxFirst: 'Connect your mailbox above before you can launch this team.',
      liveTitle: 'Your team is live 🎉',
      liveBody: '"{{name}}" is up and running and ready to take on real requests.',
      // Email teams only. Deploying publishes the team; it still watches
      // nothing until the automatic-runs switch is on, and that switch is off
      // by default.
      liveEmailNext:
        "One more step: switch on automatic runs below and it'll start watching your inbox.",
      adjust: 'Make changes',
      // The two ways off this screen. A team that answers questions is
      // finished, so the offer is to talk to it; an email team still has its
      // switch and limits here, so its button only closes the build.
      tryIt: 'Try it out',
      done: 'Done',
    },
  },
  // The org's one mailbox and its one automatic team. Both components render
  // inside the wizard's Go live step, but both are ORG-scoped rather than
  // team-scoped -- which is what most of this copy exists to say out loud.
  email: {
    connect: {
      title: 'Connect your mailbox',
      checking: 'Checking mailbox…',
      retry: 'Retry',
      subtitle:
        'This team reads and drafts email in your inbox. It only ever saves drafts for you to '
        + 'review — it never sends. Use an app-specific password, not your account password.',
      // Split either side of the address so it can stay bold mid-sentence in
      // both languages' word order (there is no <Trans> in this app).
      connectedPrefix: 'Connected as ',
      connectedSuffix: ' on {{host}}.',
      sharedByEveryTeam:
        'One mailbox is connected per organisation, and it is used by every team in your '
        + 'organisation. Reconnecting a different address moves all of them to it.',
      reconnect: 'Reconnect',
      disconnect: 'Disconnect',
      disconnecting: 'Disconnecting…',
      hostingLegend: 'How is this mailbox hosted?',
      hostingImap: 'Standard mailbox (IMAP) — Gmail, and most providers',
      hostingMicrosoft: 'Microsoft 365 / Outlook (Exchange Online)',
      // Split around the literal permission name, which is not translated:
      // the reader has to match it by sight against Azure.
      microsoftHintBefore:
        'Microsoft 365 no longer allows app passwords, so this connects through an app '
        + 'registration instead. Ask your IT administrator to register an app in Azure, grant '
        + 'it the ',
      microsoftHintAfter:
        ' permission with admin consent, and give it access to this mailbox in Exchange '
        + 'Online. They will then have the three values below.',
      emailAddress: 'Email address',
      tenantId: 'Directory (tenant) ID',
      clientId: 'Application (client) ID',
      clientSecret: 'Client secret',
      secretExpiry: 'Secret expiry date (optional)',
      secretExpiryHint:
        'Azure shows this beside the secret you just copied. Every client secret expires, and '
        + 'when one does the mailbox stops working with an error that looks like a wrong '
        + 'password — enter the date and we’ll warn you a month beforehand.',
      imapServer: 'IMAP server',
      imapUsername: 'Email address / username',
      appPassword: 'App password',
      advancedShow: '▸ Advanced settings',
      advancedHide: '▾ Advanced settings',
      port: 'IMAP port',
      portHint:
        'Almost always 993 — leave as-is unless your email provider says otherwise.',
      draftsFolder: 'Drafts folder',
      draftsPlaceholder: 'Leave blank',
      draftsHint:
        "Leave blank — we'll find your Drafts folder automatically. Only set this to force "
        + 'a specific folder.',
      test: 'Test connection',
      testing: 'Testing…',
      testOk: 'Connection works.',
      save: 'Connect mailbox',
      saving: 'Connecting…',
      // The two warnings about the one-mailbox-per-org replacement: the banner
      // while typing, then the dialog that makes it an answer.
      switchWarning:
        'This will switch every team over to {{address}}, because your organisation uses one '
        + 'mailbox for all of them. Automatic runs will be turned off until you switch them on '
        + 'again for the team you want.',
      switchConfirmTitle: 'Switch every team to this mailbox?',
      switchConfirmBody:
        'Your organisation uses one mailbox for all of its teams. Saving this replaces '
        + '{{current}} with {{address}} everywhere, and automatic runs will be turned off until '
        + 'you switch them on again for the team you want.',
      switchConfirmAction: 'Switch mailbox',
    },
    trigger: {
      title: 'Automatic runs',
      subtitle:
        'Let "{{name}}" watch the inbox on its own: it checks for new email every few minutes '
        + 'and drafts replies without you having to start it — up to {{cap}} automatic runs '
        + 'per day. It still only ever saves drafts; it never sends.',
      takenByOtherTeam:
        '"{{name}}" is the team running automatically right now. Only one team per organisation '
        + 'can, so turning this on stops "{{name}}".',
      pausedCap:
        "Paused — today's limit of {{cap}} automatic runs was reached. Runs resume tomorrow.",
      turnOff: 'Turn off automatic runs',
      turnOn: 'Run automatically when new email arrives',
      saving: 'Saving…',
      watching: 'On — watching for new email.',
    },
  },
  // The "My teams" page. It lives under pages/wizard/ but is its own route
  // rather than a wizard stage, so its strings sit outside `wizard`.
  myTeams: {
    subtitle: "Pick up where you left off, or make adjustments to a team you've already built.",
    empty: 'No teams yet — build one to see it here.',
    statusLive: 'Live',
    statusInProgress: 'In Progress',
    statusHelp: 'What does {{status}} mean?',
    explainLive: 'Live — this team is deployed and ready for your organisation to use.',
    explainInProgress:
      "Still being built — you're designing, reviewing, or trying out this team before making it live.",
    // Short forms of the Activity page's automation statuses, for a one-line
    // card tag rather than the full status block that page shows.
    automationActive: 'Automation on — watching for new email',
    automationPausedCap: 'Automation paused — daily limit reached',
    automationError: 'Automation problem — checking mailbox',
    automationDisabled: 'Automation paused',
    updated: 'Updated {{when}}',
    sharedSessions: 'Shared sessions',
    deleteTitle: 'Delete "{{name}}"?',
    deleteBody: "This can't be undone.",
  },
  // The public share-link chat (pages/ShareChatPage.tsx). The English values
  // are verbatim what the page showed before it was translated -- tests find
  // the composer and the fallback replies by these phrases.
  share: {
    placeholder: 'Type a message…',
    composerLabel: 'Your message',
    send: 'Send',
    sendHint: 'Enter to send · Shift+Enter for a new line',
    unavailable: 'This share link is no longer available.',
    loadFailed: "Couldn't load this conversation.",
    rateLimited: "Today's message limit has been reached — try again tomorrow.",
    tooLong: 'That message is too long. Please keep it under {{max}} characters.',
    pendingTurn: 'Please wait for the previous reply to finish.',
    sendFailed: 'Something went wrong sending your message. Please try again.',
    recovered: 'Something went wrong. Please try sending your message again.',
    // The backend persists these two replies in English (share_transcript.py
    // `_FALLBACK_REPLY`, share_chat.py `_DISPATCH_FAILED_MESSAGE`); the page
    // recognises them by value and renders these instead (lib/shareTraceEvents.ts).
    fallbackReply: 'Sorry, something went wrong producing a reply.',
    dispatchFailedReply: "Couldn't start a reply just now. Please try sending your message again.",
    // runtime.py's `_mark_cancelled` persists this one when a turn is stopped.
    stoppedReply: 'This conversation was stopped before a reply was ready.',
    stop: 'Stop',
    stopping: 'Stopping…',
    // Anonymous progress: a position, never a name (components/ShareProgress.tsx).
    stepProgress: 'Step {{n}} of {{total}}',
    // Deliberately generic -- never a tool or agent name (lib/shareTraceEvents.ts).
    status: {
      sending: 'Sending your message…',
      starting: 'Getting started…',
      working: 'Working on your question…',
      checking: 'Checking with the team…',
      composing: 'Putting together a reply…',
      default: 'Working on it…',
    },
  },
  // The org-side "Share" panel on each deployed team card (components/ShareLinksPanel.tsx).
  shareLinks: {
    toggle: 'Share',
    generate: 'Generate a new link',
    messagesPerDay: 'Messages per day',
    invalidCap: 'Messages per day must be a whole number from 1 to 1000.',
    expiresOn: 'Expires on (optional)',
    active: 'Active',
    revoked: 'Revoked',
    // `{{n}}`, not `{{count}}`: i18next treats `count` as a plural selector
    // and would go looking for `_one`/`_other` keys. Phrased "label: value"
    // so the English needs no plural form either ("Daily limit: 1").
    perDay: 'Daily limit: {{n}}',
    expires: 'Expires {{when}}',
    noExpiry: 'No expiry',
    copyLink: 'Copy link',
    copied: 'Copied!',
    revoke: 'Revoke',
    close: 'Close',
    copyFailed: "Couldn't copy the link automatically. Select and copy it by hand.",
  },
  // The read-only audit view beside it (components/SharedSessionsPanel.tsx).
  sharedSessions: {
    back: 'Back',
    none: 'No share links for this team yet.',
    activeLink: 'Active link',
    revokedLink: 'Revoked link',
    lastActive: 'Last active {{when}}',
    turnsToday: 'Turns today: {{n}}',
    viewTranscript: 'View transcript',
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
