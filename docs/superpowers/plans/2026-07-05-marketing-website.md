# Marketing Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a static Astro marketing website at `website/` for MyBestTeam — a multi-agent AI platform — targeting business buyers.

**Architecture:** A fully static Astro site with Tailwind CSS. No runtime API calls. All content hardcoded in `.astro` components. Deployed via `npm run build` → `dist/` to Vercel or Netlify.

**Tech Stack:** Astro 4.x, @astrojs/tailwind, Tailwind CSS 3.x, Node.js (npm)

## Global Constraints

- Root dir for all commands: `C:\Projects\MyBestTeam\website\` (must `cd website` before npm commands)
- Brand color: `#4f46e5` (indigo-600)
- Nav background: `#0a0a0f` (near-black)
- English-only copy — no Chinese text anywhere
- No runtime JavaScript except the existing `<script>` inline in components that need it
- GitHub URL: `https://github.com/GavinLai2023/bestteam`
- Demo contact: `mailto:zhenwen_lai@hotmail.com?subject=MyBestTeam Demo Request`
- `npm run build` must succeed with zero errors before marking complete

---

### Task 1: Scaffold — config files and folder structure

**Files:**
- Create: `website/package.json`
- Create: `website/astro.config.mjs`
- Create: `website/tailwind.config.mjs`
- Create: `website/tsconfig.json`
- Create: `website/public/favicon.svg`
- Create: `website/src/layouts/Base.astro`
- Create: `website/src/pages/index.astro` (skeleton only)

**Interfaces:**
- Produces: Astro project runnable with `npm install && npm run dev`

- [ ] **Step 1: Create website/package.json**

```json
{
  "name": "mybestteam-website",
  "type": "module",
  "version": "0.0.1",
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "preview": "astro preview"
  },
  "dependencies": {
    "astro": "^4.15.0",
    "@astrojs/tailwind": "^5.1.0",
    "tailwindcss": "^3.4.0"
  }
}
```

- [ ] **Step 2: Create website/astro.config.mjs**

```js
import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';

export default defineConfig({
  output: 'static',
  integrations: [tailwind()],
});
```

- [ ] **Step 3: Create website/tailwind.config.mjs**

```js
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,ts}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#4f46e5',
          light: '#eef2ff',
          dark: '#4338ca',
        },
        ink: {
          DEFAULT: '#0a0a0f',
        },
      },
      fontFamily: {
        sans: ["'Segoe UI'", 'system-ui', '-apple-system', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
```

- [ ] **Step 4: Create website/tsconfig.json**

```json
{
  "extends": "astro/tsconfigs/strict",
  "compilerOptions": {
    "strictNullChecks": true,
    "allowJs": true
  }
}
```

- [ ] **Step 5: Create website/public/favicon.svg**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect width="32" height="32" rx="8" fill="#4f46e5"/>
  <text x="16" y="22" font-family="system-ui, sans-serif" font-size="18" font-weight="bold" fill="white" text-anchor="middle">M</text>
</svg>
```

- [ ] **Step 6: Create website/src/layouts/Base.astro**

```astro
---
interface Props {
  title: string;
  description: string;
}
const { title, description } = Astro.props;
---
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title}</title>
    <meta name="description" content={description} />
    <meta property="og:title" content={title} />
    <meta property="og:description" content={description} />
    <meta property="og:type" content="website" />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  </head>
  <body class="font-sans antialiased bg-white text-gray-900">
    <slot />
  </body>
</html>
```

- [ ] **Step 7: Create website/src/pages/index.astro (skeleton)**

```astro
---
import Base from '../layouts/Base.astro';
---
<Base
  title="MyBestTeam — Multi-Agent AI Platform"
  description="Deploy custom AI teams without writing orchestration code. Describe what you need; an AI Solution Architect designs the team."
>
  <p>Coming soon</p>
</Base>
```

- [ ] **Step 8: Install dependencies**

```
cd website && npm install
```
Expected: `node_modules/` created, no errors.

- [ ] **Step 9: Verify dev server starts**

```
npm run dev
```
Expected: Server starts at http://localhost:4321, page shows "Coming soon".

- [ ] **Step 10: Commit scaffold**

```
git add website/
git commit -m "feat(website): scaffold Astro + Tailwind marketing site"
```

---

### Task 2: Nav and Hero components

**Files:**
- Create: `website/src/components/Nav.astro`
- Create: `website/src/components/Hero.astro`
- Modify: `website/src/pages/index.astro`

**Interfaces:**
- Consumes: `Base.astro` layout from Task 1
- Produces: Dark sticky nav + hero section with English headline and CTA buttons

- [ ] **Step 1: Create website/src/components/Nav.astro**

```astro
---
---
<nav class="sticky top-0 z-50 bg-ink border-b border-white/10">
  <div class="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
    <a href="/" class="text-white font-bold text-lg tracking-tight">MyBestTeam</a>
    <div class="flex items-center gap-6">
      <a
        href="https://github.com/GavinLai2023/bestteam"
        target="_blank"
        rel="noopener noreferrer"
        class="text-white/75 hover:text-white text-sm transition-colors"
      >
        GitHub
      </a>
      <a
        href="#cta"
        class="text-sm px-4 py-2 border border-white/80 text-white rounded-full hover:bg-white hover:text-ink transition-all"
      >
        Request Demo
      </a>
    </div>
  </div>
</nav>
```

- [ ] **Step 2: Create website/src/components/Hero.astro**

```astro
---
---
<section class="bg-gradient-to-b from-white to-brand-light py-24 px-6">
  <div class="max-w-4xl mx-auto text-center">
    <div class="inline-flex items-center gap-2 bg-brand-light text-brand text-xs font-semibold tracking-wide uppercase px-4 py-1.5 rounded-full mb-8 border border-brand/20">
      Multi-Agent AI Platform
    </div>
    <h1 class="text-4xl sm:text-5xl lg:text-6xl font-bold text-gray-900 leading-tight tracking-tight mb-6">
      Deploy Custom AI Teams —<br class="hidden sm:block" />
      Without Writing Orchestration Code
    </h1>
    <p class="text-xl text-gray-500 max-w-2xl mx-auto mb-10 leading-relaxed">
      Describe what you need. An AI Solution Architect designs the team.
      Your team is live in minutes.
    </p>
    <div class="flex flex-col sm:flex-row gap-4 justify-center mb-12">
      <a
        href="#cta"
        class="px-8 py-3.5 bg-brand text-white font-semibold rounded-lg hover:bg-brand-dark transition-colors text-sm"
      >
        Request Demo
      </a>
      <a
        href="https://github.com/GavinLai2023/bestteam"
        target="_blank"
        rel="noopener noreferrer"
        class="px-8 py-3.5 border border-gray-300 text-gray-700 font-semibold rounded-lg hover:border-gray-400 transition-colors text-sm"
      >
        View on GitHub
      </a>
    </div>
    <div class="flex flex-wrap justify-center gap-x-6 gap-y-2 text-sm text-gray-400">
      <span>3 collaboration modes</span>
      <span class="hidden sm:inline text-gray-300">·</span>
      <span>4 built-in tools</span>
      <span class="hidden sm:inline text-gray-300">·</span>
      <span>Zero orchestration code</span>
    </div>
  </div>
</section>
```

- [ ] **Step 3: Update website/src/pages/index.astro**

```astro
---
import Base from '../layouts/Base.astro';
import Nav from '../components/Nav.astro';
import Hero from '../components/Hero.astro';
---
<Base
  title="MyBestTeam — Multi-Agent AI Platform"
  description="Deploy custom AI teams without writing orchestration code. Describe what you need; an AI Solution Architect designs the team."
>
  <Nav />
  <Hero />
</Base>
```

- [ ] **Step 4: Verify in browser**

Run `npm run dev`. Check:
- Nav is dark (`#0a0a0f`), "MyBestTeam" in white, pill button visible
- Hero has gradient background, large English headline, two CTA buttons
- Stat strip shows 3 items

- [ ] **Step 5: Commit**

```
git add website/src/
git commit -m "feat(website): add dark Nav and Hero sections"
```

---

### Task 3: Features and Team Builder Highlight

**Files:**
- Create: `website/src/components/Features.astro`
- Create: `website/src/components/TeamBuilderHighlight.astro`
- Modify: `website/src/pages/index.astro`

**Interfaces:**
- Consumes: Nothing from prior tasks (self-contained sections)
- Produces: 4-card feature grid and 2-column wizard highlight section

- [ ] **Step 1: Create website/src/components/Features.astro**

```astro
---
const features = [
  {
    icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg>`,
    title: 'AI Designs Your Team',
    desc: 'Describe your use case; the Solution Architect agent configures the right agents, tools, and workflow automatically.',
  },
  {
    icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" /></svg>`,
    title: 'Start from a Discovery Call',
    desc: "Upload a client interview recording; it's transcribed and turned into a ready-to-review team brief automatically.",
  },
  {
    icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5" /></svg>`,
    title: 'Watch It Work in Real Time',
    desc: 'Every agent handoff streams live to your dashboard — transparent, auditable, never a black box.',
  },
  {
    icon: `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" /></svg>`,
    title: 'Deployed for Your Client',
    desc: 'Each client gets their own isolated environment with their own auth, data, and model configuration.',
  },
];
---
<section class="py-20 px-6 bg-white">
  <div class="max-w-6xl mx-auto">
    <div class="text-center mb-14">
      <h2 class="text-3xl font-bold text-gray-900 mb-4">Everything your clients need. Nothing they don't.</h2>
      <p class="text-gray-500 max-w-xl mx-auto">Built for consultants and teams who need to deploy AI automation without months of engineering work.</p>
    </div>
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
      {features.map((f) => (
        <div class="bg-gray-50 rounded-xl p-6 border border-gray-100 hover:border-brand/30 hover:shadow-sm transition-all">
          <div class="w-10 h-10 bg-brand-light rounded-lg flex items-center justify-center text-brand mb-4">
            <Fragment set:html={f.icon} />
          </div>
          <h3 class="font-semibold text-gray-900 mb-2">{f.title}</h3>
          <p class="text-sm text-gray-500 leading-relaxed">{f.desc}</p>
        </div>
      ))}
    </div>
  </div>
</section>
```

- [ ] **Step 2: Create website/src/components/TeamBuilderHighlight.astro**

```astro
---
const steps = [
  { n: '01', label: 'Describe your intent in plain language' },
  { n: '02', label: 'AI proposes the team — agents, roles, and tools' },
  { n: '03', label: 'Review with a live test run before committing' },
  { n: '04', label: 'Deploy with one click' },
];
---
<section class="py-20 px-6 bg-gray-50">
  <div class="max-w-6xl mx-auto grid grid-cols-1 lg:grid-cols-2 gap-16 items-center">
    <div>
      <div class="text-brand text-xs font-semibold tracking-widest uppercase mb-4">Team Builder Wizard</div>
      <h2 class="text-3xl font-bold text-gray-900 mb-6 leading-tight">
        Non-technical users build<br />production AI teams
      </h2>
      <p class="text-gray-500 mb-8 leading-relaxed">
        No YAML. No Python. No orchestration code. Anyone on your team can configure and deploy a custom AI workflow in under ten minutes.
      </p>
      <div class="space-y-4">
        {steps.map((s) => (
          <div class="flex items-start gap-4">
            <span class="text-brand font-mono text-sm font-bold w-8 flex-shrink-0 pt-0.5">{s.n}</span>
            <span class="text-gray-700 text-sm leading-relaxed">{s.label}</span>
          </div>
        ))}
      </div>
    </div>
    <div class="bg-white rounded-2xl border border-gray-200 shadow-lg overflow-hidden">
      <div class="bg-ink px-6 py-4">
        <div class="text-white/50 text-xs mb-2 font-mono">Team Builder</div>
        <div class="flex gap-2">
          {['Intent', 'Preview', 'Confirm', 'Deploy'].map((label, i) => (
            <div class="flex-1">
              <div class:list={['h-1 rounded-full', i === 0 ? 'bg-brand' : i === 1 ? 'bg-brand/40' : 'bg-white/15']}></div>
              <div class:list={['text-xs mt-1.5', i === 0 ? 'text-white' : 'text-white/40']}>{label}</div>
            </div>
          ))}
        </div>
      </div>
      <div class="px-6 py-5">
        <div class="text-xs text-gray-400 mb-2 font-medium">What should your AI team accomplish?</div>
        <div class="bg-gray-50 border border-gray-200 rounded-lg p-3 text-sm text-gray-500 min-h-[4rem] mb-4">
          Automate our weekly client report — research industry news, summarise key trends, and draft a 1-page brief…
        </div>
        <div class="flex gap-2">
          <div class="h-8 w-28 bg-gray-100 rounded-md"></div>
          <div class="h-8 w-28 bg-brand rounded-md"></div>
        </div>
      </div>
    </div>
  </div>
</section>
```

- [ ] **Step 3: Update website/src/pages/index.astro**

```astro
---
import Base from '../layouts/Base.astro';
import Nav from '../components/Nav.astro';
import Hero from '../components/Hero.astro';
import Features from '../components/Features.astro';
import TeamBuilderHighlight from '../components/TeamBuilderHighlight.astro';
---
<Base
  title="MyBestTeam — Multi-Agent AI Platform"
  description="Deploy custom AI teams without writing orchestration code. Describe what you need; an AI Solution Architect designs the team."
>
  <Nav />
  <Hero />
  <Features />
  <TeamBuilderHighlight />
</Base>
```

- [ ] **Step 4: Verify in browser**

Check: 4 feature cards in a grid, wizard mockup shows dark header with progress bar.

- [ ] **Step 5: Commit**

```
git add website/src/
git commit -m "feat(website): add Features and TeamBuilderHighlight sections"
```

---

### Task 4: Collaboration Modes and How It Works

**Files:**
- Create: `website/src/components/CollaborationModes.astro`
- Create: `website/src/components/HowItWorks.astro`
- Modify: `website/src/pages/index.astro`

**Interfaces:**
- Produces: 3-card collaboration mode section with CSS diagrams; 3-step numbered flow

- [ ] **Step 1: Create website/src/components/CollaborationModes.astro**

```astro
---
---
<section class="py-20 px-6 bg-white">
  <div class="max-w-6xl mx-auto">
    <div class="text-center mb-14">
      <h2 class="text-3xl font-bold text-gray-900 mb-4">Three ways agents work together</h2>
      <p class="text-gray-500 max-w-lg mx-auto">Choose the collaboration pattern that fits your workflow — or let the AI Architect pick for you.</p>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">

      <div class="border border-gray-200 rounded-xl p-6 hover:border-brand/40 transition-colors">
        <div class="text-xs font-semibold text-emerald-600 tracking-widest uppercase mb-3">Step by step</div>
        <div class="flex items-center gap-2 mb-6 py-4 justify-center">
          <div class="px-3 py-1.5 bg-gray-100 rounded text-xs font-mono text-gray-600">Agent A</div>
          <svg class="text-gray-300 w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 16 16"><path fill="currentColor" d="M8.5 3.5L13 8l-4.5 4.5-.7-.7 3.3-3.3H3V7.5h8.1L7.8 4.2l.7-.7z"/></svg>
          <div class="px-3 py-1.5 bg-gray-100 rounded text-xs font-mono text-gray-600">Agent B</div>
          <svg class="text-gray-300 w-4 h-4 flex-shrink-0" fill="none" viewBox="0 0 16 16"><path fill="currentColor" d="M8.5 3.5L13 8l-4.5 4.5-.7-.7 3.3-3.3H3V7.5h8.1L7.8 4.2l.7-.7z"/></svg>
          <div class="px-3 py-1.5 bg-brand-light border border-brand/20 rounded text-xs font-mono text-brand">Agent C</div>
        </div>
        <h3 class="font-semibold text-gray-900 mb-2">Sequential</h3>
        <p class="text-sm text-gray-500 leading-relaxed">Agents pass work down a pipeline — ideal for research → draft → review workflows.</p>
      </div>

      <div class="border border-gray-200 rounded-xl p-6 hover:border-brand/40 transition-colors">
        <div class="text-xs font-semibold text-brand tracking-widest uppercase mb-3">All at once</div>
        <div class="flex flex-col items-center gap-1.5 mb-6 py-2">
          <div class="px-3 py-1.5 bg-gray-100 rounded text-xs font-mono text-gray-600">Input</div>
          <div class="flex gap-8">
            <div class="w-px h-4 bg-gray-200"></div>
            <div class="w-px h-4 bg-gray-200"></div>
            <div class="w-px h-4 bg-gray-200"></div>
          </div>
          <div class="flex gap-2">
            <div class="px-2.5 py-1 bg-gray-100 rounded text-xs font-mono text-gray-600">A</div>
            <div class="px-2.5 py-1 bg-gray-100 rounded text-xs font-mono text-gray-600">B</div>
            <div class="px-2.5 py-1 bg-gray-100 rounded text-xs font-mono text-gray-600">C</div>
          </div>
          <div class="flex gap-8">
            <div class="w-px h-4 bg-gray-200"></div>
            <div class="w-px h-4 bg-gray-200"></div>
            <div class="w-px h-4 bg-gray-200"></div>
          </div>
          <div class="px-3 py-1.5 bg-brand-light border border-brand/20 rounded text-xs font-mono text-brand">Merged</div>
        </div>
        <h3 class="font-semibold text-gray-900 mb-2">Parallel</h3>
        <p class="text-sm text-gray-500 leading-relaxed">Agents tackle sub-tasks simultaneously; results are aggregated — cuts latency on independent work.</p>
      </div>

      <div class="border border-gray-200 rounded-xl p-6 hover:border-brand/40 transition-colors">
        <div class="text-xs font-semibold text-violet-600 tracking-widest uppercase mb-3">Led by a manager</div>
        <div class="flex flex-col items-center gap-2 mb-6 py-2">
          <div class="px-3 py-1.5 bg-violet-50 border border-violet-200 rounded text-xs font-mono text-violet-700">Manager</div>
          <div class="flex gap-8">
            <div class="w-px h-4 bg-gray-200"></div>
            <div class="w-px h-4 bg-gray-200"></div>
          </div>
          <div class="flex gap-2">
            <div class="px-2.5 py-1 bg-gray-100 rounded text-xs font-mono text-gray-600">Spec A</div>
            <div class="px-2.5 py-1 bg-gray-100 rounded text-xs font-mono text-gray-600">Spec B</div>
          </div>
        </div>
        <h3 class="font-semibold text-gray-900 mb-2">Hierarchical</h3>
        <p class="text-sm text-gray-500 leading-relaxed">A manager agent delegates to specialists and synthesises their output — mirrors real team structures.</p>
      </div>

    </div>
  </div>
</section>
```

- [ ] **Step 2: Create website/src/components/HowItWorks.astro**

```astro
---
const steps = [
  {
    n: '1',
    title: 'Describe your need',
    desc: 'Type what you want the team to accomplish — or upload a client discovery recording for automatic brief generation.',
  },
  {
    n: '2',
    title: 'AI designs the team',
    desc: 'The Solution Architect proposes agents, assigns roles and tools, and selects the right collaboration mode. You review and confirm.',
  },
  {
    n: '3',
    title: 'Deploy and monitor',
    desc: 'One Docker Compose command launches your team. The monitoring dashboard streams every agent action live so nothing is a black box.',
  },
];
---
<section class="py-20 px-6 bg-gray-50">
  <div class="max-w-4xl mx-auto text-center">
    <h2 class="text-3xl font-bold text-gray-900 mb-4">From idea to deployed team in three steps</h2>
    <p class="text-gray-500 mb-14 max-w-lg mx-auto">No orchestration expertise required. The platform handles the complexity.</p>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-8 text-left">
      {steps.map((s) => (
        <div>
          <div class="w-10 h-10 rounded-full bg-brand text-white font-bold text-sm flex items-center justify-center mb-4">{s.n}</div>
          <h3 class="font-semibold text-gray-900 mb-2">{s.title}</h3>
          <p class="text-sm text-gray-500 leading-relaxed">{s.desc}</p>
        </div>
      ))}
    </div>
  </div>
</section>
```

- [ ] **Step 3: Update website/src/pages/index.astro**

```astro
---
import Base from '../layouts/Base.astro';
import Nav from '../components/Nav.astro';
import Hero from '../components/Hero.astro';
import Features from '../components/Features.astro';
import TeamBuilderHighlight from '../components/TeamBuilderHighlight.astro';
import CollaborationModes from '../components/CollaborationModes.astro';
import HowItWorks from '../components/HowItWorks.astro';
---
<Base
  title="MyBestTeam — Multi-Agent AI Platform"
  description="Deploy custom AI teams without writing orchestration code. Describe what you need; an AI Solution Architect designs the team."
>
  <Nav />
  <Hero />
  <Features />
  <TeamBuilderHighlight />
  <CollaborationModes />
  <HowItWorks />
</Base>
```

- [ ] **Step 4: Commit**

```
git add website/src/
git commit -m "feat(website): add CollaborationModes and HowItWorks sections"
```

---

### Task 5: CTA, Footer, final index.astro, and deployment config

**Files:**
- Create: `website/src/components/CallToAction.astro`
- Create: `website/src/components/Footer.astro`
- Modify: `website/src/pages/index.astro` (final composition)
- Create: `website/vercel.json`
- Create: `website/netlify.toml`
- Create: `website/README.md`

**Interfaces:**
- Produces: Complete, fully-composed landing page; deployment configs for Vercel and Netlify

- [ ] **Step 1: Create website/src/components/CallToAction.astro**

```astro
---
---
<section id="cta" class="py-20 px-6 bg-brand">
  <div class="max-w-2xl mx-auto text-center">
    <h2 class="text-3xl font-bold text-white mb-4">Ready to build your first AI team?</h2>
    <p class="text-white/70 mb-8 text-lg">Start with a free demo or explore the open-source SDK.</p>
    <div class="flex flex-col sm:flex-row gap-4 justify-center">
      <a
        href="mailto:zhenwen_lai@hotmail.com?subject=MyBestTeam Demo Request"
        class="px-8 py-3.5 bg-white text-brand font-semibold rounded-lg hover:bg-gray-50 transition-colors text-sm"
      >
        Request Demo
      </a>
      <a
        href="https://github.com/GavinLai2023/bestteam"
        target="_blank"
        rel="noopener noreferrer"
        class="px-8 py-3.5 border border-white/50 text-white font-semibold rounded-lg hover:border-white hover:bg-white/10 transition-all text-sm"
      >
        View on GitHub
      </a>
    </div>
  </div>
</section>
```

- [ ] **Step 2: Create website/src/components/Footer.astro**

```astro
---
---
<footer class="py-8 px-6 bg-white border-t border-gray-200">
  <div class="max-w-6xl mx-auto flex flex-col sm:flex-row items-center justify-between gap-4">
    <p class="text-sm text-gray-400">© 2025 MyBestTeam</p>
    <a
      href="https://github.com/GavinLai2023/bestteam"
      target="_blank"
      rel="noopener noreferrer"
      class="text-sm text-gray-400 hover:text-gray-600 transition-colors"
    >
      GitHub
    </a>
  </div>
</footer>
```

- [ ] **Step 3: Write final website/src/pages/index.astro**

```astro
---
import Base from '../layouts/Base.astro';
import Nav from '../components/Nav.astro';
import Hero from '../components/Hero.astro';
import Features from '../components/Features.astro';
import TeamBuilderHighlight from '../components/TeamBuilderHighlight.astro';
import CollaborationModes from '../components/CollaborationModes.astro';
import HowItWorks from '../components/HowItWorks.astro';
import CallToAction from '../components/CallToAction.astro';
import Footer from '../components/Footer.astro';
---
<Base
  title="MyBestTeam — Multi-Agent AI Platform"
  description="Deploy custom AI teams without writing orchestration code. Describe what you need; an AI Solution Architect designs the team."
>
  <Nav />
  <Hero />
  <Features />
  <TeamBuilderHighlight />
  <CollaborationModes />
  <HowItWorks />
  <CallToAction />
  <Footer />
</Base>
```

- [ ] **Step 4: Create website/vercel.json**

```json
{
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "framework": "astro"
}
```

- [ ] **Step 5: Create website/netlify.toml**

```toml
[build]
  command   = "npm run build"
  publish   = "dist"
```

- [ ] **Step 6: Create website/README.md**

```markdown
# MyBestTeam — Marketing Website

Static Astro site. No runtime dependency on `ui/backend/`.

## Local dev

cd website && npm install && npm run dev   # http://localhost:4321

## Build

npm run build   # output in website/dist/

## Deploy

- **Vercel**: import the repo, set root directory to `website/`.
- **Netlify**: set base directory to `website/`, build command `npm run build`, publish dir `dist`.
```

- [ ] **Step 7: Run final build**

```
cd website && npm run build
```
Expected: `dist/` generated, zero errors, zero unexpected JS bundles.

- [ ] **Step 8: Verify checklist**
  - [ ] Nav is dark (`#0a0a0f`), white text
  - [ ] Hero has English headline, no Chinese text anywhere
  - [ ] "Request Demo" nav link scrolls to `#cta`
  - [ ] 3 collaboration mode CSS diagrams render (sequential arrows, parallel fan-out, hierarchical manager)
  - [ ] `dist/` exists after build

- [ ] **Step 9: Commit**

```
git add website/
git commit -m "feat(website): complete marketing landing page with CTA, Footer, and deployment config"
```
