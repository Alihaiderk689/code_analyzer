import { getPasswordChecks } from '../lib/validation'

const RULES = [
  ['length', '8-128 characters'],
  ['uppercase', 'An uppercase letter'],
  ['lowercase', 'A lowercase letter'],
  ['number', 'A number'],
  ['special', 'A special character'],
]

export default function PasswordChecklist({ password }) {
  const checks = getPasswordChecks(password)
  return (
    <div className="pw-checklist">
      {RULES.map(([key, label]) => (
        <div key={key} className={`pw-check-item${checks[key] ? ' met' : ''}`}>
          {checks[key] ? '✓' : '○'} {label}
        </div>
      ))}
    </div>
  )
}
