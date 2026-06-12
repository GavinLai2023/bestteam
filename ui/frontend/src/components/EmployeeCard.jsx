// A "virtual employee" card -- avatar placeholder + friendly job title +
// one-line description. Falls back to the technical agent fields
// (`name`/`role`/`goal`) if the Solution Architect didn't fill in the
// friendly `display_name`/`friendly_description`.
export default function EmployeeCard({ agent }) {
  if (!agent) return null

  const name = agent.display_name || agent.name
  const description = agent.friendly_description || agent.goal
  const initial = name.trim().charAt(0).toUpperCase() || '?'

  return (
    <div className="employee-card">
      <div className="employee-avatar">{initial}</div>
      <div className="employee-name">{name}</div>
      <div className="employee-role">{agent.role}</div>
      <p className="employee-description">{description}</p>
    </div>
  )
}
