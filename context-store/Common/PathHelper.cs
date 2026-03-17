using System;
using System.IO;

namespace Hekate.Common
{
    public static class PathHelper
    {
        public static string ValidateAndCombine(string rootPath, string userPath)
        {
            if (string.IsNullOrWhiteSpace(rootPath))
            {
                throw new ArgumentNullException(nameof(rootPath));
            }

            if (string.IsNullOrWhiteSpace(userPath))
            {
                throw new ArgumentNullException(nameof(userPath));
            }

            var combinedPath = Path.Combine(rootPath, userPath);
            var fullRootPath = Path.GetFullPath(rootPath);
            var fullCombinedPath = Path.GetFullPath(combinedPath);

            if (!fullCombinedPath.StartsWith(fullRootPath, StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("Path traversal detected.", nameof(userPath));
            }

            return fullCombinedPath;
        }
    }
}
