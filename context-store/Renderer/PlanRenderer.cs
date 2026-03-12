// CodeStoragePoc - Plan Renderer
//
// Reads a plan node tree from PostgreSQL and renders it as human-readable text.
// Complements CSharpGenerator (which renders code nodes) — this renders planning nodes.
//
// The renderer walks the tree depth-first, emitting formatted text based on node_type:
//   plan       → title + metadata
//   plan_phase → section header with status
//   plan_step  → numbered item with status indicator
//   task       → sub-item under a step
//   risk       → risk entry with severity and mitigation
//   question   → question with proposed answer
//   test_spec  → test description with type
//   retrospective → placeholder or filled content
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.Renderer;

public class PlanRenderer
{
    private readonly NodeRepository _repo;

    public PlanRenderer(NodeRepository repo) => _repo = repo;

    /// <summary>Render a plan tree as readable text.</summary>
    public async Task<string> Render(Guid planRootId)
    {
        var tree = await _repo.GetSubtree(planRootId);
        if (tree == null) return "(plan not found)";

        var sb = new System.Text.StringBuilder();
        RenderNode(sb, tree, 0);
        return sb.ToString();
    }

    private void RenderNode(System.Text.StringBuilder sb, TreeNode node, int depth)
    {
        var indent = new string(' ', depth * 2);

        switch (node.Record.NodeType)
        {
            case "plan":
                sb.AppendLine($"{'='.Repeat(60)}");
                sb.AppendLine($"PLAN: {node.Record.Name}");
                sb.AppendLine($"{'='.Repeat(60)}");
                sb.AppendLine($"Level:   {node.Attr("level")}");
                sb.AppendLine($"Status:  {node.Attr("status")}");
                sb.AppendLine($"Created: {node.Attr("created_date")}");
                sb.AppendLine($"Origin:  {node.Attr("origin")}");
                sb.AppendLine();

                // Render phases first, then risks, then questions
                var phases = node.Children.Where(c => c.Record.NodeType == "plan_phase").ToList();
                var risks = node.Children.Where(c => c.Record.NodeType == "risk").ToList();
                var questions = node.Children.Where(c => c.Record.NodeType == "question").ToList();

                foreach (var phase in phases)
                    RenderNode(sb, phase, depth);

                if (risks.Count > 0)
                {
                    sb.AppendLine($"{'-'.Repeat(40)}");
                    sb.AppendLine("RISKS");
                    sb.AppendLine($"{'-'.Repeat(40)}");
                    foreach (var risk in risks)
                        RenderNode(sb, risk, depth + 1);
                    sb.AppendLine();
                }

                if (questions.Count > 0)
                {
                    sb.AppendLine($"{'-'.Repeat(40)}");
                    sb.AppendLine("OPEN QUESTIONS");
                    sb.AppendLine($"{'-'.Repeat(40)}");
                    foreach (var q in questions)
                        RenderNode(sb, q, depth + 1);
                    sb.AppendLine();
                }
                break;

            case "plan_phase":
                var phaseStatus = StatusIcon(node.Attr("status"));
                sb.AppendLine($"{'-'.Repeat(40)}");
                sb.AppendLine($"{phaseStatus} Phase: {node.Record.Name}");
                sb.AppendLine($"{'-'.Repeat(40)}");
                if (node.Record.Value != null)
                    sb.AppendLine($"  {node.Record.Value}");
                sb.AppendLine();

                foreach (var child in node.Children)
                    RenderNode(sb, child, depth + 1);
                break;

            case "plan_step":
                var stepStatus = StatusIcon(node.Attr("status"));
                sb.AppendLine($"{indent}{stepStatus} {node.Record.Name}");
                if (node.Record.Value != null)
                    sb.AppendLine($"{indent}  {node.Record.Value}");

                foreach (var child in node.Children)
                    RenderNode(sb, child, depth + 1);
                break;

            case "task":
                var taskStatus = StatusIcon(node.Attr("status"));
                sb.AppendLine($"{indent}{taskStatus} {node.Record.Name}");
                if (node.Record.Value != null)
                    sb.AppendLine($"{indent}    {node.Record.Value}");
                break;

            case "risk":
                var severity = node.Attr("severity", "?");
                sb.AppendLine($"{indent}[{severity.ToUpper()}] {node.Record.Value}");
                var mitigation = node.Attr("mitigation");
                if (!string.IsNullOrEmpty(mitigation))
                    sb.AppendLine($"{indent}  Mitigation: {mitigation}");
                break;

            case "question":
                sb.AppendLine($"{indent}? {node.Record.Value}");
                var proposed = node.Attr("proposed_answer");
                if (!string.IsNullOrEmpty(proposed))
                    sb.AppendLine($"{indent}  Proposed: {proposed}");
                var qStatus = node.Attr("status");
                if (!string.IsNullOrEmpty(qStatus))
                    sb.AppendLine($"{indent}  Status: {qStatus}");
                break;

            case "test_spec":
                var testType = node.Attr("test_type", "?");
                var testStatus = StatusIcon(node.Attr("status"));
                sb.AppendLine($"{indent}{testStatus} [{testType}] {node.Record.Value}");
                break;

            case "retrospective":
                var retroStatus = node.Attr("status");
                if (retroStatus == "pending")
                    sb.AppendLine($"{indent}(retrospective pending)");
                else
                {
                    sb.AppendLine($"{indent}RETROSPECTIVE: {node.Record.Name}");
                    if (node.Record.Value != null)
                        sb.AppendLine($"{indent}  {node.Record.Value}");
                }
                break;

            default:
                // Unknown node type — emit as generic
                sb.AppendLine($"{indent}[{node.Record.NodeType}] {node.Record.Name ?? node.Record.Value ?? "(unnamed)"}");
                foreach (var child in node.Children)
                    RenderNode(sb, child, depth + 1);
                break;
        }
    }

    private static string StatusIcon(string status) => status switch
    {
        "completed" => "[done]",
        "in_progress" => "[....]",
        "pending" => "[    ]",
        "proposed" => "[prop]",
        "blocked" => "[BLKD]",
        _ => "[    ]"
    };
}

// Extension for string repeat (not built-in in older C#)
internal static class StringExtensions
{
    public static string Repeat(this char c, int count) => new(c, count);
}
