import unittest
from unittest.mock import patch, MagicMock
import json
import os
import sys

# Add the project root to the python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
sys.path.insert(0, project_root)

from backend.services.sentinel.reasoner import Reasoner
from backend.services.sentinel.models import PlanState, ReasoningResult
from backend.services.openai_service import OpenAIService

class TestReasoner(unittest.TestCase):

    @patch('backend.services.sentinel.reasoner.OpenAIService')
    def test_reason_model_not_found_recommends_reassign_tier(self, MockOpenAIService):
        # Arrange
        mock_openai_service = MockOpenAIService.return_value
        
        # This is what the LLM is expected to return for a "model not found" error
        llm_response_json = {
            "analysis": "The task failed because the specified model was not found. This suggests the current tier is incorrect or unavailable.",
            "next_steps": ["Check task 1"],
            "confidence": 0.9,
            "fix_type": "reassign_tier",
            "fix_params": {
                "new_tier": "tier-2"
            }
        }
        mock_openai_service.chat_completion.return_value = json.dumps(llm_response_json)

        reasoner = Reasoner(openai_service=mock_openai_service)
        
        plan_state = PlanState(
            plan_id=1,
            max_stuck_count=3,
            active_tasks=1,
            stuck_tasks=1,
            completed_tasks=0,
            failed_tasks=1,
            task_errors={"task1": "500 Server Error for '...' - Model `claude-3-opus-20240229` not found."},
            task_tiers={"task1": "tier-1"}
        )

        # Act
        result = reasoner.reason(plan_state, "cascade_failure")

        # Assert
        self.assertIsInstance(result, ReasoningResult)
        self.assertEqual(result.fix_type, "reassign_tier")
        self.assertIn("new_tier", result.fix_params)
        self.assertEqual(result.fix_params["new_tier"], "tier-2")

        # Also assert that the prompt passed to the LLM contained the error
        mock_openai_service.chat_completion.assert_called_once()
        call_args = mock_openai_service.chat_completion.call_args
        # The prompt is constructed from a system and a user message. We check the user message content.
        prompt = call_args.kwargs['messages'][1]['content']
        self.assertIn("Model `claude-3-opus-20240229` not found.", prompt)
        self.assertIn("task_errors", prompt)

if __name__ == '__main__':
    unittest.main()
