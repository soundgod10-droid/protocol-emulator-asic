* Probe: is an I/O cell SPICE file available to the simulator? If it
* loads, the deck listing gives the .subckt lines of the input pad cells
* (for the input pad delay of the host SCK limit, docs/host_port.md 6).
.lib cornerMOSlv.lib mos_tt
* "sg13cmos5l_io.spi" was not found; the standard cells use ".spice".
.include sg13cmos5l_io.spice
V1 a 0 0
R1 a 0 1k
.control
listing deck > io_deck_listing.txt
op
.endc
.end
