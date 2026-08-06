import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import '../components/WizardLayout.css'

export default function LoginPage() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (!username.trim() || !password || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const { access_token } = await api.login(username.trim(), password)
      localStorage.setItem('bestteam_token', access_token)
      navigate('/')
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="wizard-card" style={{ maxWidth: 360, margin: '64px auto' }}>
      <h2>Log in</h2>

      {error && <div className="banner banner-error">{error}</div>}

      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="username">Username</label>
          <input
            id="username"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            disabled={submitting}
            autoFocus
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
          />
        </div>

        <div className="wizard-actions">
          <button type="submit" className="btn btn-primary" disabled={!username.trim() || !password || submitting}>
            {submitting ? 'Logging in…' : 'Log in'}
          </button>
        </div>
      </form>
    </div>
  )
}
