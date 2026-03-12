//  Orchestration Engine - .NET Assembly Reflector
//
//  Loads a .NET assembly via MetadataLoadContext (no execution) and extracts
//  type metadata as structured JSON for AI-driven code decomposition.
//
//  Depends on: System.Reflection.MetadataLoadContext
//  Used by:    backend/tools/dotnet_reflection.py

using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;

if (args.Length < 1)
{
    Console.Error.WriteLine("Usage: dotnet-reflector <assembly-path> [--namespace <filter>]");
    return 1;
}

var assemblyPath = args[0];
string? nsFilter = null;

for (int i = 1; i < args.Length - 1; i++)
{
    if (args[i] == "--namespace" && i + 1 < args.Length)
        nsFilter = args[i + 1];
}

if (!File.Exists(assemblyPath))
{
    Console.Error.WriteLine($"Assembly not found: {assemblyPath}");
    return 1;
}

try
{
    var result = ReflectAssembly(assemblyPath, nsFilter);
    var options = new JsonSerializerOptions
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
    };
    Console.Write(JsonSerializer.Serialize(result, options));
    return 0;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"Reflection failed: {ex.Message}");
    return 1;
}

static AssemblyInfo ReflectAssembly(string assemblyPath, string? nsFilter)
{
    // Resolve runtime assemblies for MetadataLoadContext
    var runtimeDir = Path.GetDirectoryName(typeof(object).Assembly.Location)!;
    var resolver = new PathAssemblyResolver(
        Directory.GetFiles(runtimeDir, "*.dll")
            .Append(assemblyPath)
    );

    using var mlc = new MetadataLoadContext(resolver);
    var assembly = mlc.LoadFromAssemblyPath(assemblyPath);

    var classes = new List<ClassInfo>();

    foreach (var type in assembly.GetTypes())
    {
        if (type.IsCompilerGenerated())
            continue;

        if (nsFilter != null && type.Namespace?.StartsWith(nsFilter) != true)
            continue;

        // Only process classes, structs, and interfaces
        if (!type.IsClass && !type.IsValueType && !type.IsInterface)
            continue;

        var classInfo = new ClassInfo
        {
            Name = type.Name,
            Namespace = type.Namespace,
            Kind = type.IsInterface ? "interface" : type.IsValueType ? "struct" : "class",
            BaseClass = type.BaseType?.Name is "Object" or "ValueType" ? null : type.BaseType?.Name,
            Interfaces = type.GetInterfaces()
                .Where(i => !i.IsCompilerGenerated())
                .Select(i => FormatTypeName(i))
                .ToList(),
            Constructors = type.GetConstructors(BindingFlags.Public | BindingFlags.Instance)
                .Select(c => new ConstructorInfo_
                {
                    Parameters = c.GetParameters().Select(ToParamInfo).ToList(),
                })
                .ToList(),
            Methods = type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic
                                     | BindingFlags.Instance | BindingFlags.Static
                                     | BindingFlags.DeclaredOnly)
                .Where(m => !m.IsSpecialName && !m.IsCompilerGenerated())
                .Select(m => new MethodInfo_
                {
                    Name = m.Name,
                    ReturnType = FormatTypeName(m.ReturnType),
                    Parameters = m.GetParameters().Select(ToParamInfo).ToList(),
                    Accessibility = m.IsPublic ? "public" : m.IsFamily ? "protected"
                        : m.IsPrivate ? "private" : "internal",
                    IsStatic = m.IsStatic ? true : null,
                    IsAsync = m.ReturnType.Name.StartsWith("Task") ? true : null,
                    Signature = FormatMethodSignature(m),
                })
                .ToList(),
            Properties = type.GetProperties(BindingFlags.Public | BindingFlags.Instance
                                            | BindingFlags.DeclaredOnly)
                .Select(p => new PropertyInfo_
                {
                    Name = p.Name,
                    Type = FormatTypeName(p.PropertyType),
                    HasGetter = p.GetMethod != null,
                    HasSetter = p.SetMethod != null,
                })
                .ToList(),
        };

        // Drop empty lists for cleaner output
        if (classInfo.Interfaces.Count == 0) classInfo.Interfaces = null;
        if (classInfo.Constructors.Count == 0) classInfo.Constructors = null;
        if (classInfo.Properties.Count == 0) classInfo.Properties = null;

        classes.Add(classInfo);
    }

    return new AssemblyInfo
    {
        AssemblyName = assembly.GetName().Name ?? Path.GetFileNameWithoutExtension(assemblyPath),
        Classes = classes,
    };
}

static ParamInfo ToParamInfo(ParameterInfo p) => new()
{
    Name = p.Name ?? "arg",
    Type = FormatTypeName(p.ParameterType),
    HasDefault = p.HasDefaultValue ? true : null,
};

static string FormatTypeName(Type t)
{
    if (t.IsGenericType)
    {
        var name = t.Name.Split('`')[0];
        var args = string.Join(", ", t.GetGenericArguments().Select(FormatTypeName));
        return $"{name}<{args}>";
    }
    return t.Name switch
    {
        "String" => "string",
        "Int32" => "int",
        "Int64" => "long",
        "Boolean" => "bool",
        "Single" => "float",
        "Double" => "double",
        "Decimal" => "decimal",
        "Void" => "void",
        "Object" => "object",
        _ => t.Name,
    };
}

static string FormatMethodSignature(MethodInfo m)
{
    var access = m.IsPublic ? "public" : m.IsFamily ? "protected"
        : m.IsPrivate ? "private" : "internal";
    var staticMod = m.IsStatic ? " static" : "";
    var asyncMod = m.ReturnType.Name.StartsWith("Task") ? " async" : "";
    var returnType = FormatTypeName(m.ReturnType);
    var parameters = string.Join(", ",
        m.GetParameters().Select(p => $"{FormatTypeName(p.ParameterType)} {p.Name}"));
    return $"{access}{staticMod}{asyncMod} {returnType} {m.Name}({parameters})";
}

static class TypeExtensions
{
    public static bool IsCompilerGenerated(this Type t) =>
        t.Name.StartsWith('<') || t.Name.Contains("__") ||
        t.GetCustomAttributesData().Any(a =>
            a.AttributeType.Name == "CompilerGeneratedAttribute");

    public static bool IsCompilerGenerated(this MethodInfo m) =>
        m.Name.StartsWith('<') ||
        m.GetCustomAttributesData().Any(a =>
            a.AttributeType.Name == "CompilerGeneratedAttribute");
}

// --- JSON output models ---

record AssemblyInfo
{
    public string AssemblyName { get; init; } = "";
    public List<ClassInfo> Classes { get; init; } = new();
}

record ClassInfo
{
    public string Name { get; init; } = "";
    public string? Namespace { get; init; }
    public string Kind { get; init; } = "class";
    public string? BaseClass { get; init; }
    public List<string>? Interfaces { get; set; }
    public List<ConstructorInfo_>? Constructors { get; set; }
    public List<MethodInfo_> Methods { get; init; } = new();
    public List<PropertyInfo_>? Properties { get; set; }
}

record ConstructorInfo_
{
    public List<ParamInfo> Parameters { get; init; } = new();
}

record MethodInfo_
{
    public string Name { get; init; } = "";
    public string ReturnType { get; init; } = "void";
    public List<ParamInfo> Parameters { get; init; } = new();
    public string Accessibility { get; init; } = "public";
    public bool? IsStatic { get; init; }
    public bool? IsAsync { get; init; }
    public string Signature { get; init; } = "";
}

record PropertyInfo_
{
    public string Name { get; init; } = "";
    public string Type { get; init; } = "";
    public bool HasGetter { get; init; }
    public bool HasSetter { get; init; }
}

record ParamInfo
{
    public string Name { get; init; } = "";
    public string Type { get; init; } = "";
    public bool? HasDefault { get; init; }
}
