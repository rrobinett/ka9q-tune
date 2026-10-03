"""ka9q-tune: verify and maintain the CPU conditions radiod needs at high sample rates.

Copyright (C) 2026 the ka9q-tune contributors.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
A PARTICULAR PURPOSE. See the LICENSE file for the full text.


Design rule, from the specification this implements: never report success from
configuration. Every claim this package makes is backed by a reading taken after
the fact, and every external path or command is injectable so the whole thing is
testable without a machine.
"""

__version__ = "0.1.0"
