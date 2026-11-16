# Copyright 2025 LibreLane Contributors
#
# Adapted from OpenLane
#
# Copyright 2020-2022 Efabless Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ---------------------------------------------------------------------------
# Changes for this project (SPDX-License-Identifier: Apache-2.0): the macro grid
# of LibreLane 3.1.0.dev4 scripts/openroad/common/pdn_cfg.tcl is replaced by the
# full-height macro stripes and their check at the end of this file.
# ---------------------------------------------------------------------------
#
# Power grid for the IHP SG13 SRAM macros on an SG13CMOS5L tile.
#
# A tile has one stripe layer, Metal4, and the macros have their supply pins on
# Metal4 as vertical columns. LibreLane's default macro grid connects a macro
# through a Metal4-to-TopMetal1 via, but a tile may not use TopMetal1 (layout
# precheck), so a stripe can only reach a macro by running over its columns.
# pdngen cuts every stripe at a FIXED instance, which would leave the macro
# unpowered and the cut stripe ends far from the tile edge (the precheck wants
# every Metal4 supply port within 10 um of the top and bottom edges). So the
# stripes run the full height: the grid is built with the macros marked PLACED
# and their status is restored before anything is written. Then a check stops
# the step if a stripe over a macro is not inside supply columns of its own net,
# or if a macro supply pin has no stripe, and check_power_grid runs on every
# supply net.
#
# Geometry (macro LEF, 512x16 and 1024x16 have the same column positions):
# VDD!/VDDARRAY! columns 2.81 um wide at local x 4.26 + 11.24 k, VSS! at
# 9.88 + 11.24 k. FP_PDN_VWIDTH 2.1 (precheck minimum port width), VSPACING 3.52
# (GROUND stripe 5.62 um right of POWER, the column distance), VPITCH 67.44
# (6 x 11.24, steps over the irregular middle band), VOFFSET 25.40 (first POWER
# stripe at core x 2.88 + 25.40 = 28.28 = 11.04 + 17.24). Macros must sit at
# x = 11.04 + 67.44 k, orientation N or FS.

source $::env(SCRIPTS_DIR)/openroad/common/io.tcl
source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl
set_global_connections

set secondary []
foreach vdd $::env(VDD_NETS) gnd $::env(GND_NETS) {
    if { $vdd != $::env(VDD_NET)} {
        lappend secondary $vdd

        set db_net [[ord::get_db_block] findNet $vdd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $vdd]
            $net setSpecial
            $net setSigType "POWER"
        }
    }

    if { $gnd != $::env(GND_NET)} {
        lappend secondary $gnd

        set db_net [[ord::get_db_block] findNet $gnd]
        if {$db_net == "NULL"} {
            set net [odb::dbNet_create [ord::get_db_block] $gnd]
            $net setSpecial
            $net setSigType "GROUND"
        }
    }
}

set_voltage_domain -name CORE -power $::env(VDD_NET) -ground $::env(GND_NET) \
    -secondary_power $secondary

if { $::env(PDN_MULTILAYER) == 1 } {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_HORIZONTAL_LAYER) \
        -width $::env(PDN_HWIDTH) \
        -pitch $::env(PDN_HPITCH) \
        -offset $::env(PDN_HOFFSET) \
        -spacing $::env(PDN_HSPACING) \
        -starts_with POWER \
        {*}$arg_list

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
} else {

    set arg_list [list]
    if { $::env(PDN_ENABLE_PINS) } {
        lappend arg_list -pins "$::env(PDN_VERTICAL_LAYER)"
    }

    define_pdn_grid \
        -name stdcell_grid \
        -starts_with POWER \
        -voltage_domain CORE \
        {*}$arg_list

    set arg_list [list]
    append_if_equals arg_list PDN_EXTEND_TO "core_ring" -extend_to_core_ring
    append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary

    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_VERTICAL_LAYER) \
        -width $::env(PDN_VWIDTH) \
        -pitch $::env(PDN_VPITCH) \
        -offset $::env(PDN_VOFFSET) \
        -spacing $::env(PDN_VSPACING) \
        -starts_with POWER \
        {*}$arg_list
}

# Adds the standard cell rails if enabled.
if { $::env(PDN_ENABLE_RAILS) == 1 } {
    add_pdn_stripe \
        -grid stdcell_grid \
        -layer $::env(PDN_RAIL_LAYER) \
        -width $::env(PDN_RAIL_WIDTH) \
        -followpins

    add_pdn_connect \
        -grid stdcell_grid \
        -layers "$::env(PDN_RAIL_LAYER) $::env(PDN_VERTICAL_LAYER)"
}

# Adds the core ring if enabled.
if { $::env(PDN_CORE_RING) == 1 } {
    if { $::env(PDN_MULTILAYER) == 1 } {
        set arg_list [list]
        append_if_flag arg_list PDN_CORE_RING_ALLOW_OUT_OF_DIE -allow_out_of_die
        append_if_flag arg_list PDN_CORE_RING_CONNECT_TO_PADS -connect_to_pads
        append_if_equals arg_list PDN_EXTEND_TO "boundary" -extend_to_boundary
        append_if_exists_argument arg_list PDN_CORE_RING_CONNECT_TO_PAD_LAYERS -connect_to_pad_layers

        set pdn_core_vertical_layer $::env(PDN_VERTICAL_LAYER)
        set pdn_core_horizontal_layer $::env(PDN_HORIZONTAL_LAYER)

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            set pdn_core_vertical_layer $::env(PDN_CORE_VERTICAL_LAYER)
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            set pdn_core_horizontal_layer $::env(PDN_CORE_HORIZONTAL_LAYER)
        }

        add_pdn_ring \
            -grid stdcell_grid \
            -layers "$pdn_core_vertical_layer $pdn_core_horizontal_layer" \
            -widths "$::env(PDN_CORE_RING_VWIDTH) $::env(PDN_CORE_RING_HWIDTH)" \
            -spacings "$::env(PDN_CORE_RING_VSPACING) $::env(PDN_CORE_RING_HSPACING)" \
            -core_offset "$::env(PDN_CORE_RING_VOFFSET) $::env(PDN_CORE_RING_HOFFSET)" \
            {*}$arg_list

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_HORIZONTAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_HORIZONTAL_LAYER) $::env(PDN_VERTICAL_LAYER)"
        }

        if { [info exists ::env(PDN_CORE_VERTICAL_LAYER)] && [info exists ::env(PDN_CORE_HORIZONTAL_LAYER)] } {
            add_pdn_connect \
                -grid stdcell_grid \
                -layers "$::env(PDN_CORE_VERTICAL_LAYER) $::env(PDN_CORE_HORIZONTAL_LAYER)"
        }

    } else {
        throw APPLICATION "PDN_CORE_RING cannot be used when PDN_MULTILAYER is set to false."
    }
}

# ---------------------------------------------------------------------------
# Macro stripes (this project). No macro grid is defined: the stdcell grid's
# Metal4 stripes run through the macro supply columns.
# ---------------------------------------------------------------------------

# A stripe may pass over a macro stretch without a same-net pin below it only
# between two same-net pins and for at most this length (um). The VDD!/VDDARRAY!
# break in a POWER column is 6.64 um.
set ::macro_pdn_max_gap 7.0

# Supply pin rectangles of all macros on the stripe layer: a list of
# {net pin_key x1 y1 x2 y2} in die database units.
proc macro_pdn_columns { block layer macros_var keys_var } {
    upvar $macros_var macros
    upvar $keys_var keys
    set cols [list]
    foreach inst [$block getInsts] {
        if { ![[$inst getMaster] isBlock] } { continue }
        set name [$inst getName]
        set orient [$inst getOrient]
        if { $orient ni {R0 MX} } {
            error "MACROPDN: $name has orientation $orient; only N (R0) and FS (MX) keep its supply columns vertical"
        }
        set bb [$inst getBBox]
        lappend macros [list $name [$bb xMin] [$bb yMin] [$bb xMax] [$bb yMax]]
        foreach iterm [$inst getITerms] {
            set mterm [$iterm getMTerm]
            if { [$mterm getSigType] ni {POWER GROUND} } { continue }
            set key "$name/[$mterm getName]"
            set net [$iterm getNet]
            if { $net eq "NULL" } {
                error "MACROPDN: $key has no net (PDN_MACRO_CONNECTIONS)"
            }
            dict set keys $key 0
            # Master pin boxes moved to die coordinates: N keeps y, FS (MX) mirrors it.
            foreach mpin [$mterm getMPins] {
                foreach box [$mpin getGeometry] {
                    if { [$box isVia] } { continue }
                    if { [[$box getTechLayer] getName] ne $layer } { continue }
                    set x1 [expr {[$bb xMin] + [$box xMin]}]
                    set x2 [expr {[$bb xMin] + [$box xMax]}]
                    if { $orient eq "R0" } {
                        set y1 [expr {[$bb yMin] + [$box yMin]}]
                        set y2 [expr {[$bb yMin] + [$box yMax]}]
                    } else {
                        set y1 [expr {[$bb yMax] - [$box yMax]}]
                        set y2 [expr {[$bb yMax] - [$box yMin]}]
                    }
                    lappend cols [list $net $key $x1 $y1 $x2 $y2]
                }
            }
        }
    }
    return $cols
}

proc macro_pdn_check {} {
    set block [ord::get_db_block]
    set layer $::env(PDN_VERTICAL_LAYER)
    set dbu [$block getDbUnitsPerMicron]
    set gap_max [expr {round($::macro_pdn_max_gap * $dbu)}]
    set macros [list]
    set keys [dict create]
    set cols [macro_pdn_columns $block $layer macros keys]
    if { [llength $macros] == 0 } {
        puts "MACROPDN no macros"
        return
    }
    foreach m $macros {
        lassign $m name x0 y0 x1 y1
        puts [format "MACROPDN macro %s bbox %.3f %.3f %.3f %.3f" $name \
            [expr {double($x0) / $dbu}] [expr {double($y0) / $dbu}] \
            [expr {double($x1) / $dbu}] [expr {double($y1) / $dbu}]]
    }

    set errors [list]
    foreach net [$block getNets] {
        if { ![$net isSpecial] } { continue }
        foreach swire [$net getSWires] {
            foreach w [$swire getWires] {
                if { [$w isVia] } { continue }
                if { [[$w getTechLayer] getName] ne $layer } { continue }
                set sx1 [$w xMin]; set sy1 [$w yMin]; set sx2 [$w xMax]; set sy2 [$w yMax]
                foreach m $macros {
                    lassign $m name mx0 my0 mx1 my1
                    if { $sx2 <= $mx0 || $sx1 >= $mx1 || $sy2 <= $my0 || $sy1 >= $my1 } { continue }
                    set lo [expr {max($sy1, $my0)}]
                    set hi [expr {min($sy2, $my1)}]
                    set what [format "%s x %.3f-%.3f over %s" [$net getName] \
                        [expr {double($sx1) / $dbu}] [expr {double($sx2) / $dbu}] $name]
                    # Same-net pin shapes whose x range contains the stripe, sorted by y.
                    set under [list]
                    foreach c $cols {
                        lassign $c cnet key px1 py1 px2 py2
                        if { $cnet ne $net || $px1 > $sx1 || $px2 < $sx2 } { continue }
                        if { $py2 <= $lo || $py1 >= $hi } { continue }
                        lappend under [list $py1 $py2 $key]
                    }
                    set under [lsort -integer -index 0 $under]
                    set y $lo
                    set used [list]
                    set fault ""
                    foreach u $under {
                        lassign $u py1 py2 key
                        if { $py1 > $y } {
                            if { $y == $lo || $py1 - $y > $gap_max } {
                                set fault [format "no same-net pin under y %.3f-%.3f" \
                                    [expr {double($y) / $dbu}] [expr {double($py1) / $dbu}]]
                                break
                            }
                        }
                        set y [expr {max($y, $py2)}]
                        lappend used $key
                    }
                    if { $fault eq "" && $y < $hi } {
                        set fault [format "no same-net pin under y %.3f-%.3f" \
                            [expr {double($y) / $dbu}] [expr {double($hi) / $dbu}]]
                    }
                    if { $fault ne "" } {
                        lappend errors "$what: $fault"
                        continue
                    }
                    foreach key [lsort -unique $used] { dict incr keys $key }
                    puts "MACROPDN stripe $what inside [join [lsort -unique $used] { + }]"
                }
            }
        }
    }
    foreach e $errors { puts "MACROPDN ERROR $e" }
    if { [llength $errors] > 0 } {
        error "MACROPDN: [llength $errors] stripe(s) over a macro not inside same-net supply pins"
    }
    set missing [list]
    dict for {key n} $keys {
        puts "MACROPDN $key $n stripe(s)"
        if { $n == 0 } { lappend missing $key }
    }
    if { [llength $missing] > 0 } {
        error "MACROPDN: no stripe on [join $missing {, }]"
    }
}

# pdngen runs check_setup (needs every macro FIXED), build_grids (makes
# obstructions of FIXED instances), write_to_db and reset_shapes. The wrapper
# runs the same four steps, with the macros PLACED during build_grids only.
if { [info commands ::macro_pdn_pdngen] eq "" } {
    rename ::pdngen ::macro_pdn_pdngen
    proc ::pdngen { args } {
        foreach a $args {
            if { $a ne "-skip_trim" } {
                return [::macro_pdn_pdngen {*}$args]
            }
        }
        foreach cmd {pdn::check_setup pdn::build_grids pdn::write_to_db pdn::reset_shapes} {
            if { [info commands ::$cmd] eq "" } {
                error "MACROPDN: $cmd not found in this OpenROAD"
            }
        }
        set trim [expr {[lsearch -exact $args -skip_trim] < 0}]
        ::pdn::check_setup
        set fixed [list]
        foreach inst [[ord::get_db_block] getInsts] {
            if { [[$inst getMaster] isBlock] && [$inst isFixed] } {
                lappend fixed [list $inst [$inst getPlacementStatus]]
                $inst setPlacementStatus "PLACED"
            }
        }
        set rc [catch { ::pdn::build_grids $trim } msg opts]
        foreach f $fixed {
            lassign $f inst status
            $inst setPlacementStatus $status
        }
        if { $rc } {
            return -options $opts $msg
        }
        ::pdn::write_to_db 1 ""
        ::pdn::reset_shapes
        macro_pdn_check
        foreach net_name [concat $::env(VDD_NETS) $::env(GND_NETS)] {
            puts "MACROPDN check_power_grid -net $net_name"
            check_power_grid -net $net_name
        }
    }
}
