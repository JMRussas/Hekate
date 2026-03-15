# L3 Orchestration Logic and Process

This document outlines the process for handling code modification tasks, as per the "Develop the orchestration logic for assembling context" phase of the L3 implementation plan. This serves as the blueprint for the "Glue" layer.

## Phase 1: Task Intake & Target Identification

1.  **Analyze User Request**: The initial user request (e.g., "In the `plan_project` function, add a check for the project's status") is parsed to identify the primary target.
2.  **Locate Target Chunk**:
    *   Use `grep_search` on the codebase with the target name (e.g., `plan_project`) to quickly find the relevant file(s).
    *   Consult `l3_manifest.json` to confirm the exact file path, and to get the `objectPath` (e.g., `create_server.plan_project` if it were discoverable, or just `plan_project` as it is now).

## Phase 2: Context Assembly

This phase gathers all necessary information to perform the change with maximum context and minimum noise.

1.  **Retrieve Target's Raw Code**:
    *   Using the file path and the `start_line`/`end_line` from `l3_manifest.json`, the exact source code of the target function is read using the `read_file` tool.

2.  **Perform Dependency Walk**:
    *   The sidecar for the target function is read (e.g., `.sidecars/backend/mcp/server.py/plan_project.json`).
    *   The `dependencies` array within this sidecar file is parsed. This array lists all other functions, methods, or modules that the target function relies on.

3.  **Gather Dependency Sidecars**:
    *   For each dependency identified in the previous step, its corresponding sidecar file is located and read. For example, if `plan_project` depends on the `_post` helper, its sidecar (`.sidecars/backend/mcp/server.py/_post.json`) would be loaded.
    *   This provides a "semantic-only" view of the dependencies, detailing *what* they do (their summary) without including their full source code.

## Phase 3: Prompt Construction

With all context assembled, a detailed, structured prompt is constructed for the "Worker Agent" (the code-generating persona) to execute the change. This adheres to the "Constraint Injection" principle of the L3 plan.

The prompt will follow this XML-like structure:

```xml
<ImplementationTask version="L3.1-python">
    <UserRequest>
        ${USER_INSTRUCTION}
    </UserRequest>

    <TargetScope>
        <File path="${file_path}" />
        <Object path="${object_path}" />
        <RawContent>
            <![CDATA[
            ${RAW_SOURCE_CODE_OF_TARGET}
            ]]>
        </RawContent>
    </TargetScope>

    <ReflectedContext>
        <!-- For each dependency, a block like this is added -->
        <Dependency objectPath="${dep.objectPath}">
            <Signature><![CDATA[${dep.signature}]]></Signature>
            <Summary><![CDATA[${dep.summary}]]></Summary>
        </Dependency>
        ...
    </ReflectedContext>

    <ExecutionParameters>
        <Instruction>
            Based on the user request, modify the raw code in <TargetScope>.
            You must only use the functions available in <ReflectedContext>.
            Adhere strictly to their described behavior and signatures.
            Provide only the new, complete source code for the target function.
        </Instruction>
        <StrictMode>True</StrictMode>
    </ExecutionParameters>
</ImplementationTask>
```

## Phase 4: Execution and Validation

1.  **Code Generation**: The LLM, acting as the Worker Agent, receives the structured prompt and generates the new source code for the target function ONLY.
2.  **Apply Change**: The `replace` tool is used to substitute the old function body with the newly generated one in the target source file.
3.  **Validation**: The final task, "Implement the validation and unit testing trigger," will be invoked here. This will involve running static analysis and the relevant `pytest` tests for the modified file.
