"""The CIO office: conversations, memory, read-only portfolio tools, and proposals.

The CIO reads the firm and files proposals. It cannot place orders, move
capital, change mandates or modes, or write manager activity: proposals are
applied only when the owner approves them, by firm.cio.proposals.apply.
"""

CIO_NAME = "Graham"  # the CIO's name in the product; the agent id stays "cio"
