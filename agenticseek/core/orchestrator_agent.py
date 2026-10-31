"""Single model-driven executor using NovaMind's existing tool boundary."""
from agenticseek.core.agent import Agent


class OrchestratorAgent(Agent):
    type = "orchestrator_agent"
    role = "orchestrator"

    def check_limits(self):
        super().check_limits()
        # Report budget exhaustion explicitly, including when the final allowed
        # action consumed the last model turn (rather than quietly finalizing).
        budget = self.step_budget
        if budget is not None and budget.used >= budget.limit:
            from agenticseek.core.errors import StepLimitReached
            raise StepLimitReached()

    def __init__(self, name, prompt_path, provider, tools, max_turns=20):
        super().__init__(name, prompt_path, provider)
        self.max_turns = max(1, min(int(max_turns), 500))
        for tag, tool in tools.items():
            self.register_tool(tag, tool)
