// CodeStoragePoc - Code Node Repository Contract
//
// The narrow slice of NodeRepository that CSharpDecomposer and CSharpGenerator
// actually use. It exists so those two classes can run against an in-memory
// store in tests without a database. It deliberately lists ONLY the methods
// those two classes call; do not grow it for other callers.
//
// Depends on: NodeRepository's TreeNode type
// Used by:    CSharpDecomposer, CSharpGenerator, NodeRepository (implements)

namespace CodeStoragePoc.DbLayer;

public interface ICodeNodeRepository
{
    /// <summary>Get or create a file record. Returns the file ID.</summary>
    Task<Guid> GetOrCreateFile(Guid projectId, string filePath);

    /// <summary>Delete all nodes belonging to a file.</summary>
    Task DeleteNodesByFile(Guid fileId);

    /// <summary>Insert a node with optional attributes. Returns the node ID.</summary>
    Task<Guid> InsertNode(
        Guid id, Guid projectId, Guid? fileId,
        string nodeType, string? name, string? value,
        Guid? parentId, int siblingOrder,
        string? modifiedBy = null,
        Dictionary<string, string>? attributes = null);

    /// <summary>Update a file's root_node_id.</summary>
    Task UpdateFileRootNode(Guid fileId, Guid rootNodeId);

    /// <summary>Fetch a subtree (children sorted by sibling_order, attributes loaded), or null.</summary>
    Task<TreeNode?> GetSubtree(Guid rootId);

    /// <summary>Get all nodes for a file as a flat list with attributes loaded.</summary>
    Task<List<TreeNode>> GetNodesByFile(Guid fileId);
}
