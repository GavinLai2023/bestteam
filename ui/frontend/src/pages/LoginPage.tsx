import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, TOKEN_KEY } from '../lib/api'
import BrandMark from '../components/BrandMark'
import BetaBadge from '../components/BetaBadge'
import LanguageSelect from '../components/LanguageSelect'
// `.field-box`, `.btn` and `.banner-error` live here despite the file's name --
// it is the app's shared form stylesheet, and the page has always read it.
import '../components/WizardLayout.css'
import './LoginPage.css'

// The only page outside `Layout`, so it renders its own brand and its own
// language control -- without the latter a customer who cannot read English
// has no way out of it, which is where the whole bilingual app used to start.
//
// `#username`, `#password`, `button[type=submit]` and `.banner-error` are
// load-bearing: tests/e2e/test_smoke.py drives the real page through them.
// Struck through once the password is visible. Drawn rather than set in an
// emoji: the two obvious ones (👁 / 🙈) render at the mercy of the platform's
// font and one of them is a joke.
function EyeIcon({ crossed }: { crossed: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M1 8s2.6-4.4 7-4.4S15 8 15 8s-2.6 4.4-7 4.4S1 8 1 8z" />
      <circle cx="8" cy="8" r="1.9" />
      {crossed && <path d="M2.5 2.5l11 11" />}
    </svg>
  )
}

export default function LoginPage() {
  const navigate = useNavigate()
  const { t } = useTranslation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [revealed, setRevealed] = useState(false)
  const [capsLock, setCapsLock] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!username.trim() || !password || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const { access_token } = await api.login(username.trim(), password)
      localStorage.setItem(TOKEN_KEY, access_token)
      navigate('/')
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="login-page">
      <section className="login-brand">
        <span className="login-brand-name">
          <BrandMark size={26} />
          {t('nav.brand')}
          <BetaBadge />
        </span>
        <p className="login-tagline">{t('nav.tagline')}</p>
        {/* Decoration, not information: hidden rather than stacked on a phone,
            where it would push the form below the fold. */}
        <ul className="login-points">
          <li>{t('login.points.noCode')}</li>
          <li>{t('login.points.seeEverything')}</li>
          <li>{t('login.points.share')}</li>
        </ul>
      </section>

      <section className="login-form-panel">
        <div className="login-language">
          <LanguageSelect />
        </div>

        <h1>{t('login.heading')}</h1>

        {error && <div className="banner banner-error">{error}</div>}

        <form onSubmit={submit}>
          {/* The label sits after the input and doubles as the resting
              placeholder, so CSS can float it on `:focus` / `:not(
              :placeholder-shown)`. The `placeholder=" "` is what makes the
              latter work -- a real placeholder would show through the label. */}
          <div className="field-box">
            <input
              id="username"
              type="text"
              autoComplete="username"
              placeholder=" "
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              autoFocus
            />
            <label htmlFor="username">{t('login.username')}</label>
          </div>

          <div className="field-box login-password">
            <input
              id="password"
              type={revealed ? 'text' : 'password'}
              autoComplete="current-password"
              placeholder=" "
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyUp={(e) => setCapsLock(e.getModifierState('CapsLock'))}
              disabled={submitting}
            />
            <label htmlFor="password">{t('login.password')}</label>
            <button
              type="button"
              className="login-reveal"
              aria-label={revealed ? t('login.hidePassword') : t('login.showPassword')}
              aria-pressed={revealed}
              onClick={() => setRevealed((r) => !r)}
              disabled={submitting}
            >
              <EyeIcon crossed={revealed} />
            </button>
          </div>
          {capsLock && <p className="login-caps-lock">{t('login.capsLock')}</p>}

          <button
            type="submit"
            className="btn btn-primary login-submit"
            disabled={!username.trim() || !password || submitting}
          >
            {submitting ? t('login.submitting') : t('login.submit')}
          </button>
        </form>
      </section>
    </div>
  )
}
