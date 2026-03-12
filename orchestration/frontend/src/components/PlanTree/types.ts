// Orchestration Engine - PlanTree Node Types
//
// Internal tree model that PlanTree builds from PlanData.
// Each node has a type, label, children, and optional detail payload.
//
// Depends on: theme.ts
// Used by:    PlanTree/index.tsx, PlanTree/PlanTreeNode.tsx, PlanTree/NodeDetail.tsx

import type { NodeType } from './theme'

export interface TreeNode {
  id: string
  type: NodeType
  label: string
  sublabel?: string
  badges?: Badge[]
  children: TreeNode[]
  detail?: NodeDetailData
  defaultExpanded?: boolean
  dependsOn?: string[]    // IDs of nodes this task depends on
  taskIndex?: number      // Original task index from plan data
}

export interface Badge {
  text: string
  colorKey: NodeType
}

export interface NodeDetailData {
  title: string
  sections: DetailSection[]
}

export interface DetailSection {
  label: string
  content: string | string[]
}
