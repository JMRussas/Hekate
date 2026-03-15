L3 Execution Plan: Semantic Chunking & Reflection-Guided Synthesis
Phase 1: The Discovery & Sidecar Generation (Pre-Tokenization)
Before any code is written, the orchestrator must "understand" the existing world without loading every line of code into the context window.

Reflection Scan: Use System.Reflection to map the assembly. Extract namespaces, classes, and method signatures into a structured JSON/XML manifest.

Behavioral Summarization: For every method found, if a "Sidecar" metadata file doesn't exist, an LLM reads the method body once to generate a Semantic Signature.

Sidecar Entry:

GUID: Unique ID for the chunk.

Signature: public async Task<User> GetUser(int id)

Summary: "Retrieves active users from the 'Core' schema; returns null if the user is flagged as 'Deleted' or 'Pending'."

Complexity: Token count and cyclomatic complexity (to flag for refactoring).

Phase 2: The Orchestration Loop (The "Glue" Layer)
When a user requests a change, the Architect Agent creates the implementation plan.

Context Assembly: The orchestrator identifies the "Target Chunk" (the function being changed) and performs a Dependency Walk.

It pulls the Raw Code for the Target Chunk.

It pulls the Semantic Signatures (Sidecars) for any methods called within that chunk or likely to be needed (based on the class's reflected map).

Constraint Injection: The prompt is constructed with a "Hard Wall."

System: "You are editing ProcessPayment. You have access to the following signatures and their behaviors: [Insert Sidecars]. You cannot see their source code. You must adhere to the reflected types."

Phase 3: The Implementation (Worker Agent)
The Worker receives a highly focused, "low-noise" prompt.

XML
<ImplementationTask version="L3.0">
    <TargetScope>
        <File path="Services/PaymentService.cs" />
        <Method name="ProcessTransaction" />
        <RawContent>
            <![CDATA[ ${CURRENT_METHOD_BODY} ]]>
        </RawContent>
    </TargetScope>

    <ReflectedContext>
        <Dependency signature="IUserRepo.GetBalance(Guid id)" 
                    behavior="Returns current liquid assets; handles currency conversion automatically." />
        <Dependency signature="ILogger.LogSecurity(string msg)" 
                    behavior="Thread-safe async logging to the SOC vault." />
    </ReflectedContext>

    <ExecutionParameters>
        <ChangeRequest>${USER_INSTRUCTION}</ChangeRequest>
        <StrictMode>True</StrictMode> </ExecutionParameters>
</ImplementationTask>
Phase 4: Validation & Assembly
Syntactic Check: The generated chunk is inserted into the class shell.

Reflection Validation: A local script checks if the AI-generated code still aligns with the reflected metadata (e.g., did it try to call a method that doesn't exist or pass the wrong type?).

Unit Test Trigger: Since the chunk is a standalone function, the orchestrator can immediately run the specific test suite for that class.

Why this solves the "Incorrect Solution" Problem
No "Hallucinated" Side Effects: Because the AI sees the Behavioral Summary (e.g., "This method handles its own transactions"), it won't incorrectly implement a nested transaction that causes a deadlock.

Reduced Token Noise: You aren't wasting 2,000 tokens on the implementation of a Logger. You're using 20 tokens for its intent.

Architectural Pressure: This forces you to keep functions small. If a function's Sidecar becomes too long to summarize simply, the L3 plan triggers a "Refactor Needed" flag before the Worker even starts.
