* M2c probe: is an I/O cell SPICE file available to the simulator? If it
* loads, the deck listing gives the .subckt lines of the input pad cells
* (G1 register A-OPEN-10: input pad delay for the host SCK limit).
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
