// CodeStoragePoc - Conversation Renderer
//
// Reads a conversation node tree from PostgreSQL and renders it as a threaded
// transcript. Shows turns with speaker/mode badges, topics with grouped ideas,
// and action items with status.
//
// Transport-agnostic: voice and text turns render identically except for a
// [voice] or [text] badge on each turn.
//
// Depends on: NodeRepository
// Used by:    Program

using CodeStoragePoc.DbLayer;

namespace CodeStoragePoc.Renderer;

public class ConversationRenderer
{
    private readonly NodeRepository _repo;

    public ConversationRenderer(NodeRepository repo) => _repo = repo;

    /// <summary>Render a conversation tree as readable transcript.</summary>
    public async Task<string> Render(Guid conversationRootId)
    {
        var tree = await _repo.GetSubtree(conversationRootId);
        if (tree == null) return "(conversation not found)";

        var sb = new System.Text.StringBuilder();
        RenderConversation(sb, tree);
        return sb.ToString();
    }

    private void RenderConversation(System.Text.StringBuilder sb, TreeNode node)
    {
        sb.AppendLine($"{'='.Repeat(60)}");
        sb.AppendLine($"CONVERSATION: {node.Record.Name}");
        sb.AppendLine($"{'='.Repeat(60)}");
        sb.AppendLine($"Started: {node.Attr("started_at")}");
        sb.AppendLine($"Type:    {node.Attr("session_type")}");
        sb.AppendLine();

        // Separate turns from topics/action_items
        var turns = node.Children.Where(c => c.Record.NodeType == "turn").ToList();
        var topics = node.Children.Where(c => c.Record.NodeType == "topic").ToList();
        var actionItems = node.Children.Where(c => c.Record.NodeType == "action_item").ToList();

        // Render timeline
        if (turns.Count > 0)
        {
            sb.AppendLine("TIMELINE");
            sb.AppendLine($"{'-'.Repeat(40)}");
            foreach (var turn in turns)
                RenderTurn(sb, turn);
            sb.AppendLine();
        }

        // Render extracted topics with ideas
        if (topics.Count > 0)
        {
            sb.AppendLine("EXTRACTED KNOWLEDGE");
            sb.AppendLine($"{'-'.Repeat(40)}");
            foreach (var topic in topics)
                RenderTopic(sb, topic);
            sb.AppendLine();
        }

        // Render action items
        if (actionItems.Count > 0)
        {
            sb.AppendLine("ACTION ITEMS");
            sb.AppendLine($"{'-'.Repeat(40)}");
            foreach (var item in actionItems)
                RenderActionItem(sb, item);
            sb.AppendLine();
        }
    }

    private void RenderTurn(System.Text.StringBuilder sb, TreeNode turn)
    {
        var speaker = turn.Attr("speaker", "?");
        var mode = turn.Attr("input_mode", "?");
        var badge = $"[{speaker}] [{mode}]";
        sb.AppendLine($"  {badge}");

        // Word-wrap the turn text at ~70 chars
        var text = turn.Record.Value ?? "";
        var words = text.Split(' ');
        var line = "    ";
        foreach (var word in words)
        {
            if (line.Length + word.Length > 74 && line.Length > 4)
            {
                sb.AppendLine(line);
                line = "    ";
            }
            line += word + " ";
        }
        if (line.Trim().Length > 0)
            sb.AppendLine(line.TrimEnd());
        sb.AppendLine();
    }

    private void RenderTopic(System.Text.StringBuilder sb, TreeNode topic)
    {
        var status = topic.Attr("status", "?");
        sb.AppendLine($"  Topic: {topic.Record.Name} ({status})");

        foreach (var child in topic.Children)
        {
            switch (child.Record.NodeType)
            {
                case "idea":
                    var ideaStatus = child.Attr("status", "?");
                    sb.AppendLine($"    * Idea: {child.Record.Name} [{ideaStatus}]");
                    if (child.Record.Value != null)
                        sb.AppendLine($"      {child.Record.Value}");
                    break;

                case "question":
                    var qStatus = child.Attr("status", "?");
                    sb.AppendLine($"    ? Question: {child.Record.Value} [{qStatus}]");
                    var resolution = child.Attr("resolution");
                    if (!string.IsNullOrEmpty(resolution))
                        sb.AppendLine($"      Resolution: {resolution}");
                    break;

                case "decision":
                    var dStatus = child.Attr("status", "?");
                    sb.AppendLine($"    ! Decision: {child.Record.Name} [{dStatus}]");
                    if (child.Record.Value != null)
                        sb.AppendLine($"      {child.Record.Value}");
                    break;

                default:
                    sb.AppendLine($"    [{child.Record.NodeType}] {child.Record.Name ?? child.Record.Value}");
                    break;
            }
        }
    }

    private void RenderActionItem(System.Text.StringBuilder sb, TreeNode item)
    {
        var status = StatusIcon(item.Attr("status"));
        sb.AppendLine($"  {status} {item.Record.Name}");
        if (item.Record.Value != null)
            sb.AppendLine($"    {item.Record.Value}");
    }

    private static string StatusIcon(string status) => status switch
    {
        "completed" => "[done]",
        "in_progress" => "[....]",
        "pending" => "[    ]",
        "blocked" => "[BLKD]",
        _ => "[    ]"
    };
}
