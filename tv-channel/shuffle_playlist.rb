#!/usr/bin/env ruby
files = Dir.glob("/mnt/media/*.webm").shuffle
File.open("/mnt/media/playlist.txt", "w") do |file|
  files.each { |f| file.puts "file '#{f}'" }
end
