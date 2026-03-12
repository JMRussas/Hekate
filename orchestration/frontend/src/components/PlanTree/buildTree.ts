// Orchestration Engine - PlanTree Builder
//
// Converts PlanData (from backend API) into a TreeNode hierarchy
// for rendering by PlanTree.
//
// Depends on: types.ts, ../../types/index.ts
// Used by:    PlanTree/index.tsx

import type { PlanData, PlanTask, PlanPhase, PlanOpenQuestion, PlanRisk, PlanTestStrategy } from '../../types'
import type { TreeNode, Badge } from './types'
import type { NodeType } from './theme'

function taskTypeToNode(tt: string): NodeType {
  const map: Record<string, NodeType> = {
    code: 'code', research: 'research', analysis: 'analysis',
    asset: 'asset', integration: 'integration', documentation: 'documentation',
  }
  return map[tt] ?? 'code'
}

function complexityToNode(c: string): NodeType {
  const map: Record<string, NodeType> = { simple: 'simple', medium: 'medium', complex: 'complex' }
  return map[c] ?? 'medium'
}

function buildTaskNode(task: PlanTask, index: number, phasePrefix: string, allTaskIds: Map<number, string>): TreeNode {
  const badges: Badge[] = [
    { text: task.task_type, colorKey: taskTypeToNode(task.task_type) },
    { text: task.complexity, colorKey: complexityToNode(task.complexity) },
  ]

  const depStr = task.depends_on?.length
    ? task.depends_on.map(d => typeof d === 'number' ? `Task ${d}` : d).join(', ')
    : 'none'

  const toolStr = task.tools_needed?.length ? task.tools_needed.join(', ') : 'none'

  const nodeId = `${phasePrefix}task-${index}`

  // Resolve dependency indices to node IDs
  const dependsOn = task.depends_on
    ?.map(d => typeof d === 'number' ? allTaskIds.get(d) : allTaskIds.get(parseInt(String(d), 10)))
    .filter((id): id is string => id != null) ?? []

  return {
    id: nodeId,
    type: taskTypeToNode(task.task_type),
    label: task.title,
    sublabel: task.description,
    badges,
    children: [],
    dependsOn,
    taskIndex: index,
    detail: {
      title: task.title,
      sections: [
        { label: 'Description', content: task.description },
        { label: 'Type', content: task.task_type },
        { label: 'Complexity', content: task.complexity },
        { label: 'Dependencies', content: depStr },
        { label: 'Tools', content: toolStr },
      ],
    },
  }
}

function buildPhaseNode(phase: PlanPhase, phaseIndex: number, allTaskIds: Map<number, string>): TreeNode {
  const taskNodes = phase.tasks.map((t, i) => buildTaskNode(t, i, `p${phaseIndex}-`, allTaskIds))
  return {
    id: `phase-${phaseIndex}`,
    type: 'phase',
    label: phase.name,
    sublabel: phase.description,
    badges: [{ text: `${phase.tasks.length} task${phase.tasks.length !== 1 ? 's' : ''}`, colorKey: 'phase' }],
    children: taskNodes,
    defaultExpanded: true,
  }
}

function buildQuestionNode(q: PlanOpenQuestion, index: number): TreeNode {
  return {
    id: `question-${index}`,
    type: 'question',
    label: q.question,
    sublabel: `Proposed: ${q.proposed_answer}`,
    badges: [],
    children: [],
    detail: {
      title: q.question,
      sections: [
        { label: 'Proposed Answer', content: q.proposed_answer },
        { label: 'Impact', content: q.impact },
      ],
    },
  }
}

function buildRiskNode(r: PlanRisk, index: number): TreeNode {
  return {
    id: `risk-${index}`,
    type: 'risk',
    label: r.risk,
    sublabel: `Mitigation: ${r.mitigation}`,
    badges: [
      { text: `likelihood: ${r.likelihood}`, colorKey: complexityToNode(r.likelihood) },
      { text: `impact: ${r.impact}`, colorKey: complexityToNode(r.impact) },
    ],
    children: [],
    detail: {
      title: r.risk,
      sections: [
        { label: 'Likelihood', content: r.likelihood },
        { label: 'Impact', content: r.impact },
        { label: 'Mitigation', content: r.mitigation },
      ],
    },
  }
}

function buildTestStrategyNode(ts: PlanTestStrategy): TreeNode {
  return {
    id: 'test-strategy',
    type: 'test_strategy',
    label: 'Test Strategy',
    sublabel: ts.approach,
    badges: [],
    children: [],
    detail: {
      title: 'Test Strategy',
      sections: [
        { label: 'Approach', content: ts.approach },
        ...(ts.test_tasks?.length ? [{ label: 'Test Tasks', content: ts.test_tasks }] : []),
        ...(ts.coverage_notes ? [{ label: 'Coverage Notes', content: ts.coverage_notes }] : []),
      ],
    },
  }
}

export interface BuildResult {
  tree: TreeNode
  dependencyMap: Map<string, string[]>  // nodeId → nodeIds it depends on
}

// Pre-compute a map of task index → node ID across all phases
function buildTaskIdMap(plan: PlanData): Map<number, string> {
  const map = new Map<number, string>()
  if (plan.phases && plan.phases.length > 0) {
    let globalIndex = 0
    for (let pi = 0; pi < plan.phases.length; pi++) {
      for (let ti = 0; ti < plan.phases[pi].tasks.length; ti++) {
        map.set(globalIndex, `p${pi}-task-${ti}`)
        globalIndex++
      }
    }
  } else if (plan.tasks) {
    for (let i = 0; i < plan.tasks.length; i++) {
      map.set(i, `task-${i}`)
    }
  }
  return map
}

// Collect all dependency edges from a tree into a flat map
function collectDependencies(node: TreeNode, map: Map<string, string[]>): void {
  if (node.dependsOn && node.dependsOn.length > 0) {
    map.set(node.id, node.dependsOn)
  }
  for (const child of node.children) {
    collectDependencies(child, map)
  }
}

export function buildPlanTree(plan: PlanData): BuildResult {
  const children: TreeNode[] = []
  const allTaskIds = buildTaskIdMap(plan)

  // Phases (L2/L3) or flat tasks (L1)
  if (plan.phases && plan.phases.length > 0) {
    children.push(...plan.phases.map((p, i) => buildPhaseNode(p, i, allTaskIds)))
  } else if (plan.tasks && plan.tasks.length > 0) {
    const tasksGroup: TreeNode = {
      id: 'tasks-group',
      type: 'code',
      label: 'Tasks',
      sublabel: `${plan.tasks.length} task${plan.tasks.length !== 1 ? 's' : ''}`,
      badges: [],
      children: plan.tasks.map((t, i) => buildTaskNode(t, i, '', allTaskIds)),
      defaultExpanded: true,
    }
    children.push(tasksGroup)
  }

  // Questions
  if (plan.open_questions && plan.open_questions.length > 0) {
    const questionsGroup: TreeNode = {
      id: 'questions-group',
      type: 'question',
      label: 'Open Questions',
      sublabel: `${plan.open_questions.length} question${plan.open_questions.length !== 1 ? 's' : ''}`,
      badges: [],
      children: plan.open_questions.map((q, i) => buildQuestionNode(q, i)),
      defaultExpanded: false,
    }
    children.push(questionsGroup)
  }

  // Risks
  if (plan.risk_assessment && plan.risk_assessment.length > 0) {
    const risksGroup: TreeNode = {
      id: 'risks-group',
      type: 'risk',
      label: 'Risk Assessment',
      sublabel: `${plan.risk_assessment.length} risk${plan.risk_assessment.length !== 1 ? 's' : ''}`,
      badges: [],
      children: plan.risk_assessment.map((r, i) => buildRiskNode(r, i)),
      defaultExpanded: false,
    }
    children.push(risksGroup)
  }

  // Test strategy
  if (plan.test_strategy) {
    children.push(buildTestStrategyNode(plan.test_strategy))
  }

  const tree: TreeNode = {
    id: 'plan-root',
    type: 'plan',
    label: plan.summary,
    badges: [],
    children,
    defaultExpanded: true,
  }

  const dependencyMap = new Map<string, string[]>()
  collectDependencies(tree, dependencyMap)

  return { tree, dependencyMap }
}
