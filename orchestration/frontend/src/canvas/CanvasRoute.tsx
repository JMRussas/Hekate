// Hekate canvas — route adapter.
//
// Pulls projectId from the URL, wires the production API client as the
// PlanSource deps, hands off to <ProjectCanvas/>. The point of this thin
// shim is to keep <ProjectCanvas/> ignorant of routing and API clients —
// it stays test-pure with a dependency-injected source.

import { useParams } from 'react-router-dom'
import { listTasks } from '../api/projects'
import { ProjectCanvas } from './ProjectCanvas'
import { subscribeEvents } from './subscribeEvents'

export default function CanvasRoute() {
  const { id } = useParams<{ id: string }>()
  if (!id) {
    return <div role="alert">Canvas route requires a project id in the URL.</div>
  }
  return (
    <ProjectCanvas
      projectId={id}
      deps={{
        listTasks: (projectId) => listTasks(projectId, { exclude_output: true }),
      }}
      subscribeEvents={subscribeEvents}
    />
  )
}
