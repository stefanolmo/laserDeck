
from laserdeck.gcode import BURN, RAPID, TRAVEL, clean_line, parse_program, preamble_for


def test_clean_line():
    assert clean_line("N10 G1 X1.5 Y2 (move) ; note") == "G1X1.5Y2"
    assert clean_line("%") == ""
    assert clean_line("T1 M6") == "M0"       # GRBL 1.1 rejects T / M6


def test_burn_vs_travel_kinds():
    prog = parse_program("G21G90\nM4S0\nG0X10Y0\nG1X20F1000S500\nS0\nG1X30\nM5\nG1X40\n")
    kinds = list(prog.path.kind)
    assert kinds == [RAPID, BURN, TRAVEL, TRAVEL]
    assert prog.path.power[1] == 500
    assert prog.uses_laser and prog.max_spindle == 500
    assert abs(prog.burn_length - 10) < 1e-6


def test_g0_never_burns_even_with_laser_on():
    prog = parse_program("M3S800\nG0X10\nG1X20F500\n")
    assert prog.path.kind[0] == RAPID and prog.path.kind[1] == BURN


def test_bounds_are_the_burned_area():
    prog = parse_program("G0X-50Y-50\nM4S100\nG1X0Y0F500\nG1X10Y5\nM5\nG0X200Y200\n")
    assert prog.bounds_min[:2] == (-50.0, -50.0) or prog.bounds_min[0] <= 0
    assert prog.bounds_max[0] == 10.0 and prog.bounds_max[1] == 5.0


def test_arc_burns_and_time():
    prog = parse_program("M4S300\nG1X10F600\nG2X0Y0I-5J0\n")
    assert prog.burn_length > 10 + 15       # half circle r=5 ≈ 15.7
    assert abs(prog.total_time - (prog.burn_length / 600 * 60)) < 0.5


def test_warnings():
    prog = parse_program("G1X10\nG65\n")
    assert any("no feed" in w for w in prog.warnings)
    assert any("G65" in w for w in prog.warnings)


def test_preamble_keeps_laser_off_until_positioned():
    prog = parse_program("G21G90\nM4S0\nG0X5Y5\nG1X10Y5F1200S400\nG1X20Y5\nG1X30Y5\n")
    pre = preamble_for(prog, 5)             # resume at the last line
    assert pre[:4] == ["G21", "G90", "G94", "M5"]
    i_move = next(i for i, c in enumerate(pre) if c.startswith("G0X"))
    i_laser = next(i for i, c in enumerate(pre) if c.startswith("M4"))
    assert i_move < i_laser                 # positioned (laser off) before the laser is re-enabled
    assert pre[i_move] == "G0X20.000Y5.000"
    assert pre[i_laser] == "M4S400"
