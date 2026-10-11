"""Review completeness remains independent of current disclosure authority."""

from lychd.compositions.blockworld.review import render_review
from lychd.compositions.blockworld.services import prepare_command
from tests.unit.compositions.blockworld.test_placement import admission, identity


def test_review_discloses_complete_dynamic_block_and_inventory() -> None:
    review = render_review(prepare_command(admission(), identity(11)))
    facts = {fact.key: fact.value for fact in review.facts}
    assert review.title == "Place one block on the admitted plot"
    assert facts["block_state"] == "test:stone\naxis=y"
    assert facts["inventory"] == "hotbar.1: test:stone\n2 -> 1 (delta -1)\nGeneration: 5"
    assert len(facts) == 18
